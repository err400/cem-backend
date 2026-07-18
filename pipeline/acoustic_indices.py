"""
05+06: Acoustic Indices Computation + Box Plots
=================================================
Three-part pipeline (mirrors birdnet_predictions.py architecture):
  1. File listing  — discover, filter, deduplicate WAV files
  2. Main pipeline — compute 6 acoustic indices per file, append to aggregate CSV
  3. Plotting      — per-spot box plots of each index, bucketed by hour of day

Indices computed (Section 3.2.2):
  ADI  — Acoustic Diversity Index (Shannon entropy of frequency band energy)
  ACI  — Acoustic Complexity Index (mean normalized spectral amplitude difference)
  AEI  — Acoustic Evenness Index (1 − normalized ADI)
  NDSI — Normalized Difference Soundscape Index ((bio − anthro) / (bio + anthro))
  MFC  — Mid-Frequency Cover (fraction of frames with dominant 2–8 kHz energy)
  CLS  — Cluster Count (mean spectral peak count per frame)

Performance optimizations (vs. original):
  - Hardware-adaptive parallelism via hw_profile (CPU cores, RAM)
  - Single spectrogram per file: the denoise gate and the six indices now share
    ONE STFT — no ISTFT reconstruction, no second (re-)spectrogram
  - Segmentation done in frame-space (a slice of the one spectrogram), not by
    re-transforming each 120s time-domain chunk
  - Dedicated lower analysis sample rate (no index needs energy above 11 kHz)
  - float32 spectrogram (scipy upcasts to float64 by default)
  - Frequency-band masks + max-entropy cached per worker, not rebuilt per segment
  - Faster resampling (soxr via librosa 0.11+ instead of kaiser_fast)
  - Vectorized CLS computation (eliminates per-column Python loop)
  - Pre-computed noise threshold spectrum per worker
"""

import os
import re
import numpy as np
import pandas as pd
import soundfile as sf
import librosa
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from scipy.signal import stft as scipy_stft
from scipy.stats import entropy
from datetime import date
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed

import config as cfg
from file_metadata import parse_filename, build_record
from hw_profile import get_profile, print_profile

_RESAMPLE_TYPE = "soxr_hq"

# Dedicated analysis rate for acoustic indices ONLY — never touches cfg.TARGET_SR
# (birdnet_predictions.py needs 48 kHz; its model input is fixed at 144000
# samples / 3s). Every index here uses frequencies up to 11 kHz (NDSI's bio
# band), so a 24 kHz rate (12 kHz Nyquist) covers all bands while roughly
# halving every STFT below. Override with the INDICES_SR env var if needed.
INDICES_SR = int(os.environ.get("INDICES_SR", "24000").strip() or 24000)

# Unified STFT params — used for BOTH the noise gate and the six indices, so
# there is exactly one spectral transform per file (see _analyze_file).
_STFT_NFFT, _STFT_HOP = 1024, 512


# =============================================================================
# PART 1 — FILE LISTING  (same pattern as birdnet_predictions.list_files)
# =============================================================================
def list_files(
    input_directories: list[str],
    date_start: date,
    date_end: date,
    processed_files: set[str],
    input_file_list: list[str] | None = None,
) -> list[str]:
    discovered: dict[str, str] = {}

    for directory in input_directories:
        directory = os.path.abspath(directory)
        if not os.path.isdir(directory):
            print(f"WARNING: input directory not found: {directory}")
            continue
        for root, _dirs, files in os.walk(directory):
            for fname in files:
                parsed = parse_filename(fname)
                if parsed is None:
                    continue
                if not (date_start <= parsed["date"] <= date_end):
                    continue
                if fname not in discovered:
                    discovered[fname] = os.path.join(root, fname)

    if input_file_list:
        for fpath in input_file_list:
            fpath = os.path.abspath(fpath)
            fname = os.path.basename(fpath)
            if fname not in discovered and os.path.isfile(fpath):
                discovered[fname] = fpath

    for pf in processed_files:
        discovered.pop(pf, None)

    result = sorted(discovered.values())
    print(f"File listing: {len(result)} to process ({len(processed_files)} already processed)")
    return result


# =============================================================================
# PART 2 — MAIN PIPELINE
# =============================================================================

def _noise_threshold(noise_clip: np.ndarray, sr: int) -> np.ndarray:
    """Per-frequency gate threshold (mean |STFT| * 1.2) for one noise clip.

    Length-invariant (wrap-padding just repeats the clip to match audio
    length), so computed ONCE per worker instead of per file.
    """
    _, _, Z = scipy_stft(noise_clip, fs=sr, nperseg=_STFT_NFFT, noverlap=_STFT_NFFT - _STFT_HOP)
    return (np.mean(np.abs(Z), axis=1, keepdims=True) * 1.2).astype(np.float32)


def _freq_band_masks(f: np.ndarray):
    """Boolean band masks + max-entropy, cached by caller per (sr, n_fft)."""
    anthro_mask = (f >= 1000) & (f <= 2000)
    bio_mask = (f >= 2000) & (f <= 11000)
    mid_mask = (f >= 2000) & (f <= 8000)
    return anthro_mask, bio_mask, mid_mask


_worker_freq_cache: dict = {}


def _get_freq_context(f: np.ndarray):
    """Cache masks/max-entropy keyed by spectrogram frequency-bin count.

    f and n_freq_bins are identical for every segment of every file at a fixed
    sr/n_fft, so this is computed once per worker instead of once per segment.
    """
    key = f.shape[0]
    ctx = _worker_freq_cache.get(key)
    if ctx is None:
        anthro_mask, bio_mask, mid_mask = _freq_band_masks(f)
        max_entropy = np.log(key) if key > 1 else 1.0
        ctx = (anthro_mask, bio_mask, mid_mask, max_entropy)
        _worker_freq_cache[key] = ctx
    return ctx


def _denoise_gate_spectrogram(audio: np.ndarray, noise_clips, noise_thresholds,
                              sr: int, snr_db: float | None = None):
    """Time-domain noise subtraction + ONE STFT, gated against the combined
    noise threshold. Returns (f, gated_power_spectrogram) — no ISTFT, since
    nothing downstream needs reconstructed audio.
    """
    snr_db = cfg.SNR_DB if snr_db is None else snr_db

    cleaned = audio.astype(np.float32, copy=True)
    for noise_ref in noise_clips:
        if len(noise_ref) > len(cleaned):
            nr = noise_ref[:len(cleaned)]
        else:
            nr = np.pad(noise_ref, (0, len(cleaned) - len(noise_ref)), "wrap")
        audio_power = np.mean(cleaned ** 2)
        noise_power = np.mean(nr ** 2)
        if noise_power == 0:
            continue
        desired_noise_power = audio_power / (10 ** (snr_db / 10))
        nr_scaled = nr * np.sqrt(desired_noise_power / noise_power)
        cleaned = cleaned - nr_scaled

    f, _, Z = scipy_stft(cleaned, fs=sr, nperseg=_STFT_NFFT, noverlap=_STFT_NFFT - _STFT_HOP)
    magnitude = np.abs(Z).astype(np.float32)

    combined_threshold = np.zeros((magnitude.shape[0], 1), dtype=np.float32)
    for th in noise_thresholds:
        combined_threshold = np.maximum(combined_threshold, th)

    gated_mag = np.where(magnitude > combined_threshold, magnitude, 0).astype(np.float32)
    # Square to keep the "power spectrogram" semantics the index formulas
    # expect (a global scale factor cancels in every formula below, but the
    # magnitude->power shape does not, so this must stay squared).
    Sxx = gated_mag ** 2
    return f, Sxx


# ── Index computation (vectorized CLS, shared-spectrogram) ─────────────────

def compute_acoustic_indices(Sxx: np.ndarray, freq_ctx) -> tuple:
    """Compute ADI, ACI, AEI, NDSI, MFC, CLS from a (already gated) power
    spectrogram slice. No spectral transform happens in this function — the
    caller slices one full-file spectrogram per 120s segment.
    """
    anthro_mask, bio_mask, mid_mask, max_entropy = freq_ctx
    Sxx = Sxx + np.float32(1e-10)

    # ADI: Shannon entropy of frequency band energy
    S_norm = Sxx / Sxx.sum(axis=0, keepdims=True)
    ADI = np.mean(entropy(S_norm, axis=0))

    # AEI: 1 - normalized ADI
    AEI = 1.0 - (ADI / max_entropy)

    # ACI: mean normalized absolute spectral difference
    diff = np.abs(np.diff(Sxx, axis=1))
    col_sum = Sxx[:, :-1].sum(axis=0)
    col_sum[col_sum == 0] = 1e-10
    ACI = np.mean(diff.sum(axis=0) / col_sum)

    # NDSI: (biophony - anthrophony) / (biophony + anthrophony)
    E_anthro = Sxx[anthro_mask, :].sum()
    E_bio = Sxx[bio_mask, :].sum()
    NDSI = (E_bio - E_anthro) / (E_bio + E_anthro + 1e-10)

    # MFC: mid-frequency cover (2-8 kHz > 20% of total)
    S_mid = Sxx[mid_mask, :].sum(axis=0)
    S_total = Sxx.sum(axis=0)
    MFC = np.mean(S_mid > 0.2 * S_total)

    # CLS: cluster count — vectorized (no per-column Python loop)
    frame_maxes = Sxx.max(axis=0, keepdims=True) + 1e-10
    Sxx_norm = Sxx / frame_maxes
    above_thresh = Sxx_norm[1:-1, :] > 0.5
    gt_left = Sxx_norm[1:-1, :] > Sxx_norm[:-2, :]
    gt_right = Sxx_norm[1:-1, :] > Sxx_norm[2:, :]
    peak_counts = (above_thresh & gt_left & gt_right).sum(axis=0)
    CLS = peak_counts.mean()

    return ADI, ACI, AEI, NDSI, MFC, CLS


def _analyze_file(filepath, noise_clips, noise_thresholds):
    """Load, denoise+gate (one spectrogram), segment in frame-space, compute
    indices for one WAV file.
    """
    audio_raw, orig_sr = sf.read(filepath, dtype="float32")
    if audio_raw.ndim > 1:
        audio_raw = audio_raw.mean(axis=1)
    if orig_sr != INDICES_SR:
        audio_raw = librosa.resample(y=audio_raw, orig_sr=orig_sr,
                                     target_sr=INDICES_SR, res_type=_RESAMPLE_TYPE)

    # Truncate to a whole multiple of the 120s segment length BEFORE the
    # spectral transform, so the discarded tail costs nothing (previously the
    # tail was denoised/transformed and then thrown away).
    two_min_samples = int(120 * INDICES_SR)
    n_segments = len(audio_raw) // two_min_samples
    if n_segments == 0:
        return []
    audio_raw = audio_raw[: n_segments * two_min_samples]

    f, Sxx = _denoise_gate_spectrogram(audio_raw, noise_clips, noise_thresholds, INDICES_SR)
    freq_ctx = _get_freq_context(f)

    frames_per_seg = max(1, int(round(two_min_samples / _STFT_HOP)))
    total_frames = Sxx.shape[1]
    n_complete = total_frames // frames_per_seg

    results = []
    for i in range(n_complete):
        start = i * frames_per_seg
        end = start + frames_per_seg
        seg_Sxx = Sxx[:, start:end]
        ADI, ACI, AEI, NDSI, MFC, CLS = compute_acoustic_indices(seg_Sxx, freq_ctx)
        results.append({
            "Segment": i + 1,
            "ADI": ADI, "ACI": ACI, "AEI": AEI,
            "NDSI": NDSI, "MFC": MFC, "CLS": CLS,
        })
    return results


# ── Worker state ─────────────────────────────────────────────────────────────

_worker_noise_clips = None
_worker_noise_thresholds = None


def _init_worker(noise_path):
    global _worker_noise_clips, _worker_noise_thresholds

    clip, clip_sr = sf.read(noise_path, dtype="float32")
    if clip.ndim > 1:
        clip = clip.mean(axis=1)
    if clip_sr != INDICES_SR:
        clip = librosa.resample(y=clip, orig_sr=clip_sr,
                                target_sr=INDICES_SR, res_type=_RESAMPLE_TYPE)

    _worker_noise_clips = [clip]
    _worker_noise_thresholds = [_noise_threshold(clip, INDICES_SR)]


def _process_single_file(item):
    filepath, spot_override = item
    rec = build_record(filepath, spot=spot_override)
    filename = rec["filename"]
    try:
        seg_results = _analyze_file(filepath, _worker_noise_clips, _worker_noise_thresholds)
        if seg_results:
            for r in seg_results:
                r["filename"] = rec["filename"]
                r["filepath"] = rec["filepath"]
                r["spot"] = rec["spot"]
                r["date"] = rec["date"]
                r["hour"] = rec["hour"]
            return filename, seg_results
        return filename, None
    except Exception as e:
        print(f"\n  ERROR processing {filename}: {e}")
        return filename, None


def load_processed_files(path: str) -> set[str]:
    if not os.path.isfile(path):
        return set()
    with open(path, "r") as f:
        return {line.strip() for line in f if line.strip()}


def save_processed_files(path: str, filenames: set[str]):
    with open(path, "w") as f:
        for fname in sorted(filenames):
            f.write(fname + "\n")


def run_pipeline(file_list, aggregate_path, processed_files_path, spot_overrides=None):
    spot_overrides = spot_overrides or {}
    if not file_list:
        print("No new files to process.")
        return pd.DataFrame()

    profile = get_profile()
    print_profile(profile)

    n_workers = profile["indices_workers"]
    print(f"Parallelism: {n_workers} workers ({profile['cpus']} CPUs, {profile['ram_gb']} GB RAM)")
    print(f"Analysis rate: {INDICES_SR} Hz (indices only; independent of BirdNET's 48 kHz)")

    all_results = []
    processed_this_run = set()
    already_processed = load_processed_files(processed_files_path)

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_worker,
        initargs=(cfg.STATIC_NOISE_PATH,),
    ) as executor:
        items = [(fp, spot_overrides.get(os.path.basename(fp))) for fp in file_list]
        futures = {executor.submit(_process_single_file, it): it[0] for it in items}
        with tqdm(total=len(file_list), desc="Acoustic Indices") as pbar:
            for future in as_completed(futures):
                filename, result = future.result()
                processed_this_run.add(filename)
                if result is not None:
                    all_results.extend(result)
                pbar.update(1)

    new_df = pd.DataFrame()
    if all_results:
        new_df = pd.DataFrame(all_results)
        header = not os.path.isfile(aggregate_path)
        new_df.to_csv(aggregate_path, mode="a", header=header, index=False)
        print(f"Appended {len(new_df)} rows to {aggregate_path}")
    else:
        print("No indices computed in this batch.")

    already_processed.update(processed_this_run)
    save_processed_files(processed_files_path, already_processed)
    print(f"Marked {len(processed_this_run)} files as processed (total: {len(already_processed)})")
    return new_df


# =============================================================================
# PART 3 — OUTPUT CSV + BOX PLOTS
# =============================================================================
INDICES_TO_PLOT = ["NDSI", "ADI", "ACI", "AEI", "MFC", "CLS"]


def write_output_and_plots(aggregate_path, output_dir, input_directories,
                           date_start, date_end):
    """Filter aggregate to requested range, write output CSV, generate boxplots."""
    os.makedirs(output_dir, exist_ok=True)

    if not os.path.isfile(aggregate_path):
        print("No aggregate file found.")
        return

    df = pd.read_csv(aggregate_path)
    if df.empty:
        print("Aggregate file is empty.")
        return

    if "filepath" in df.columns:
        # Vectorized prefix match instead of a per-row .apply(os.path.abspath).
        abs_dirs = [os.path.abspath(d) + os.sep for d in input_directories]
        fp_norm = df["filepath"].astype(str).str.replace("/", os.sep, regex=False)
        in_dirs = pd.Series(False, index=df.index)
        for prefix in abs_dirs:
            in_dirs |= fp_norm.str.startswith(prefix)
        in_dirs &= df["filepath"].notna()
        df = df[in_dirs]

    if "date" in df.columns:
        dts = pd.to_datetime(df["date"], errors="coerce")
        start_ts = pd.Timestamp(date_start)
        end_ts = pd.Timestamp(date_end) + pd.Timedelta(days=1)
        df = df[dts.notna() & (dts >= start_ts) & (dts < end_ts)]

    if df.empty:
        print("No data matches requested directories + date range.")
        return

    output_csv = os.path.join(output_dir, "acoustic_indices.csv")
    df.to_csv(output_csv, index=False)
    print(f"Output: {len(df)} rows -> {output_csv}")

    if "spot" in df.columns:
        df["Spot"] = df["spot"].astype(str).str.strip()
    else:
        df["Spot"] = "Unknown"

    if "hour" not in df.columns:
        print("No 'hour' column; cannot make per-hour boxplots.")
        return
    df = df[pd.to_numeric(df["hour"], errors="coerce").notna()].copy()
    df["hour"] = pd.to_numeric(df["hour"]).astype(int)

    print("Generating box plots...")
    # Group once per (index, spot) instead of re-filtering df[df.Spot==spot]
    # inside the innermost loop for every index.
    spot_groups = dict(tuple(df.groupby("Spot")))
    spots = sorted(spot_groups.keys())
    for index_name in INDICES_TO_PLOT:
        if index_name not in df.columns:
            print(f"  WARNING: {index_name} not in data, skipping.")
            continue

        for spot in spots:
            sdf = spot_groups[spot]
            if sdf.empty:
                continue

            plt.figure(figsize=(12, 6))
            sns.boxplot(
                data=sdf, x="hour", y=index_name,
                order=list(range(24)),
                palette="Set2",
            )
            plt.title(f"{index_name} by Hour - {spot}", fontsize=16)
            plt.xlabel("Hour of Day", fontsize=12)
            plt.ylabel(f"{index_name} Value", fontsize=12)
            plt.grid(axis="y", linestyle="--", alpha=0.7)
            plt.tight_layout()
            safe_spot = re.sub(r"[^\w.-]", "_", str(spot))
            plt.savefig(
                os.path.join(output_dir, f"boxplot_{index_name}_{safe_spot}.png"),
                dpi=300, bbox_inches="tight",
            )
            plt.close()

    summary_rows = []
    for index_name in INDICES_TO_PLOT:
        if index_name in df.columns:
            stats = df.groupby("Spot")[index_name].describe()[["mean", "std", "50%"]]
            stats.columns = ["Mean", "Std", "Median"]
            stats["Index"] = index_name
            summary_rows.append(stats.reset_index())
    if summary_rows:
        summary_df = pd.concat(summary_rows, ignore_index=True)
        summary_df.to_csv(os.path.join(output_dir, "index_summary_stats.csv"), index=False)

    print(f"Done. Results + plots saved to: {output_dir}")


# =============================================================================
# MAIN
# =============================================================================
def main():
    cfg.apply_overrides()
    processed_set = load_processed_files(cfg.PROCESSED_FILE_INDICES)
    files_to_process = list_files(
        input_directories=cfg.INPUT_DIRECTORIES,
        date_start=cfg.DATE_START,
        date_end=cfg.DATE_END,
        processed_files=processed_set,
        input_file_list=cfg.INPUT_FILE_LIST,
    )

    spot_overrides = {}
    spots_aligned = list(cfg.INPUT_FILE_SPOTS) + [""] * max(0, len(cfg.INPUT_FILE_LIST) - len(cfg.INPUT_FILE_SPOTS))
    for pth, sp in zip(cfg.INPUT_FILE_LIST, spots_aligned):
        base = os.path.basename(os.path.abspath(pth))
        if sp:
            spot_overrides[base] = sp

    if cfg.DATASET_SPOTS:
        ds_aligned = list(cfg.DATASET_SPOTS) + [""] * max(0, len(cfg.INPUT_DIRECTORIES) - len(cfg.DATASET_SPOTS))
        for d, s in zip(cfg.INPUT_DIRECTORIES, ds_aligned):
            if s:
                for filepath in files_to_process:
                    base = os.path.basename(filepath)
                    if base in spot_overrides:
                        continue
                    parent = os.path.dirname(os.path.abspath(filepath))
                    dir_path = os.path.abspath(d)
                    if parent == dir_path or parent.startswith(dir_path + os.sep):
                        spot_overrides[base] = s

    run_pipeline(
        file_list=files_to_process,
        aggregate_path=cfg.AGGREGATE_FILE_INDICES,
        processed_files_path=cfg.PROCESSED_FILE_INDICES,
        spot_overrides=spot_overrides,
    )

    write_output_and_plots(
        aggregate_path=cfg.AGGREGATE_FILE_INDICES,
        output_dir=cfg.OUTPUT_DIR_05_INDICES,
        input_directories=cfg.INPUT_DIRECTORIES,
        date_start=cfg.DATE_START,
        date_end=cfg.DATE_END,
    )


if __name__ == "__main__":
    main()
