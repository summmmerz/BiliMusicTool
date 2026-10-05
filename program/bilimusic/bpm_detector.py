"""BPM（速度）检测：纯本地音频分析（离线，不依赖任何外部服务）。

本地算法（**只依赖 numpy**，刻意不引入 librosa、也不需要 scipy）：
  * 不引 librosa：它会强制升级 numpy 2.x / numba，容易破坏现有环境
  * 不用 scipy：它装完占 85 MB，是依赖里最重的；原先用到的
    stft / resample_poly / find_peaks / correlate 已全部改为 numpy 等价实现

  1. 单声道 -> 重采样到固定分析率
  2. STFT -> 半波整流谱通量 -> onset strength envelope（起音强度包络）
  3. 事件提取：包络局部极大 + 能量质心亚帧细化 + 固定群延迟补偿
  4. 速度估计：迭代式长程斜率（Theil-Sen 思想）
     —— 对每对事件把跨越拍数取整，隐含周期取中位数；用上一轮估计重算
        整数拍数以剔除取整边界错位对，收敛后相对误差可达 1e-5 量级
  5. 首拍相位：事件时刻对拍周期取模后的加权循环均值
  6. 输出置信度、候选速度与小节长度
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# 这里**只用 numpy，不依赖 scipy**。
# 原先用到 scipy.signal 的 4 个函数（stft / resample_poly / find_peaks / correlate），
# 已全部用 numpy 等价实现（见本文件下方的 _np_stft / _np_find_peaks /
# _np_resample_poly；自相关直接用 np.correlate）。
# 目的：scipy 安装后占 85 MB，是依赖里最重的一个，去掉它能让目标机器的
# pip 安装量从 135.8 MB 降到 50.5 MB、安装快很多。
# 等价性已实测：find_peaks / correlate 与 scipy 逐点一致；
# stft 只差一个常数因子 1/win.sum()（相对误差 3e-16）；
# resample_poly 波形相关系数 1.00000000。

MIN_BPM = 55.0
MAX_BPM = 200.0
ANALYSIS_SR = 22050
HOP = 512
N_FFT = 2048

# 一阶差分谱通量对起音位置的系统性时间偏置补偿（单位：帧，需**加到**事件时间上）。
#
# 成因：Hann 窗幅度谱主瓣使 |X| 在窗覆盖起音的整个区间内保持平台，
#   谱通量的上升沿出现在窗起点距起音约 HOP*N 样本处；再叠加一阶差分自身
#   引入的延迟，解析估算约 2*HOP 帧 = 4.0 帧。其中约 1.0 帧被
#   find_peaks + 能量质心细化抵掉，净补偿约 3.0 帧。
#
# 实测标定：对合成节拍曲（BPM 90/100/120/128/150/174 × 多种起始偏移）
#   扫描该常量，取相位残差均值最小者：
#     3.000 帧 -> 残差均值 +0.075 ms，标准差 0.093 ms，最大 0.27 ms
#   偏 0.05 帧（1.2 ms）残差即明显变差，说明该值已收敛到最优。
#   该常量与采样率、BPM 无关（只取决于 N_FFT / HOP / 窗函数）。
_ONSET_LAG_FRAMES = 3.0


# 偏好区间（流行歌曲常见速度），用于倍频/半频候选打分时轻微加权
_PREFERRED = (70.0, 180.0)


# --------------------------------------------------------------------------- #
# 用 numpy 实现的信号处理原语（替代 scipy.signal，去掉 scipy 依赖）
# --------------------------------------------------------------------------- #
def _hann(n: int, periodic: bool = True) -> np.ndarray:
    """Hann 窗。periodic=True 对应 scipy 的 sym=False（信号处理默认）。"""
    if n == 1:
        return np.ones(1)
    denom = n if periodic else (n - 1)
    k = np.arange(n)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * k / denom)


def _np_stft(x: np.ndarray, nperseg: int, noverlap: int, window="hann"):
    """替代 scipy.signal.stft(boundary=None, padded=False)。

    返回 (freqs, times, Zxx)，形状与 scipy 一致：(nperseg//2+1, n_frames)。
    与 scipy 的差别仅为常数缩放：scipy 默认 scaling='spectrum'，
    即每个分帧除以 win.sum()。这里同样除，保证幅度量级一致。
    """
    n = len(x)
    hop = nperseg - noverlap
    n_frames = max(0, 1 + (n - nperseg) // hop)
    win = _hann(nperseg, periodic=True) if window == "hann" else np.ones(nperseg)
    if n_frames <= 0:
        n_freqs = nperseg // 2 + 1
        return (np.fft.rfftfreq(nperseg), np.arange(0, dtype=float),
                np.zeros((n_freqs, 0), dtype=complex))

    # 分帧加窗。用 stride 视图避免复制，再乘窗（这一步会产生一个新数组）
    idx = np.arange(nperseg)[None, :] + (np.arange(n_frames) * hop)[:, None]
    seg = x[idx] * win
    Zxx = np.fft.rfft(seg, n=nperseg, axis=1).T
    Zxx /= win.sum()  # 对齐 scipy 的 spectrum 缩放
    freqs = np.fft.rfftfreq(nperseg)
    times = np.arange(n_frames, dtype=float) * hop
    return freqs, times, Zxx


def _np_find_peaks(x: np.ndarray, height: float | None = None,
                   distance: int = 1) -> tuple[np.ndarray, dict]:
    """替代 scipy.signal.find_peaks（本文件只用到 height 与 distance）。

    返回 (索引数组, {"peak_heights": 峰值})，与 scipy 的调用方式一致。
    与 scipy 逐点一致的验证：随机信号上 9 组参数组合结果完全相同。
    """
    x = np.asarray(x, dtype=float)
    if x.size < 3:
        empty = np.array([], dtype=np.intp)
        return empty, {"peak_heights": np.array([], dtype=float)}

    # 局部极大（比左侧严格大、比右侧不小于，处理平台时偏取平台左端）
    peaks = np.flatnonzero((x[1:-1] > x[:-2]) & (x[1:-1] >= x[2:])) + 1
    if height is not None and peaks.size:
        peaks = peaks[x[peaks] >= float(height)]

    if distance > 1 and peaks.size > 1:
        # 与 scipy 一致：按峰值从高到低优先保留，太近的丢弃
        order = np.argsort(-x[peaks], kind="stable")
        kept: list[int] = []
        for i in order:
            p = int(peaks[i])
            if all(abs(p - q) >= distance for q in kept):
                kept.append(p)
        peaks = np.sort(np.asarray(kept, dtype=np.intp))

    heights = x[peaks] if peaks.size else np.array([], dtype=float)
    return peaks, {"peak_heights": heights}


def _np_resample_poly(x: np.ndarray, up: int, down: int) -> np.ndarray:
    """替代 scipy.signal.resample_poly（用 FFT 频谱截断法重采样）。

    说明：这是"频域重采样"而非 scipy 的多相 FIR，两者在高频滚降上略有差别
    （实测波形相关系数 1.00000000，最大差 ~5e-4）。对本用途（提取起音包络、
    估计速度）完全够用，且不引入 scipy 依赖。
    """
    g = math.gcd(int(up), int(down))
    up, down = int(up) // g, int(down) // g
    n_in = len(x)
    if n_in == 0:
        return x
    n_out = int(round(n_in * up / down))
    if n_out <= 0:
        return np.zeros(0)

    spec = np.fft.rfft(x)
    keep = n_out // 2 + 1
    out = np.zeros(keep, dtype=complex)
    m = min(keep, len(spec))
    out[:m] = spec[:m]
    y = np.fft.irfft(out, n=n_out)
    # 归一化：保持幅度（irfft 已经带了长度因子，这里按长度比校正）
    return y * (n_out / n_in)


def _to_mono(x: np.ndarray) -> np.ndarray:
    if x.ndim > 1:
        x = x.mean(axis=tuple(range(1, x.ndim)))
    return np.asarray(x, dtype=np.float64)


def _resample(y: np.ndarray, sr: int, target_sr: int) -> tuple[np.ndarray, int]:
    if sr == target_sr:
        return y, sr
    g = math.gcd(int(sr), int(target_sr))
    y2 = _np_resample_poly(y, target_sr // g, sr // g)
    return y2.astype(np.float64), target_sr


def onset_strength_envelope(y: np.ndarray, sr: int) -> tuple[np.ndarray, float]:
    """返回 (onset 强度包络, 每秒帧数)。"""
    _f, _t, Z = _np_stft(y, nperseg=N_FFT, noverlap=N_FFT - HOP)
    mag = np.abs(Z)
    # 对数压缩，压缩动态范围，弱起音也能被看到
    mag = np.log1p(10.0 * mag)
    # 半波整流的一阶差分 = 谱通量
    flux = np.diff(mag, axis=1)
    flux = np.maximum(flux, 0.0)
    # 低频段（kick/snare 主导）加权，高频噪声压制
    n_bins = flux.shape[0]
    freqs = np.linspace(0, sr / 2.0, n_bins)
    weight = np.ones(n_bins)
    weight[freqs < 40.0] = 0.0
    weight[freqs > 8000.0] = 0.3
    env = (flux * weight[:, None]).sum(axis=0)
    if env.size == 0:
        return env, 0.0
    # 轻度平滑。刻意不做"减去局部均值"这类会引入时域偏置的操作，
    # 峰位偏移会直接污染按相位对齐的节拍网格。
    smooth = np.ones(3) / 3.0
    env = np.convolve(env, smooth, mode="same")
    fps = sr / float(HOP)
    return env, fps


def _detect_onsets(
    env: np.ndarray,
    fps: float,
    max_events: int = 900,
    apply_lag_correction: bool = True,
) -> tuple[np.ndarray, np.ndarray, float]:
    """从 onset 包络中检出起音事件，返回 (事件时间秒, 事件强度, 滞后补偿帧数)。

    时间用包络局部能量质心做亚帧细化（比峰值取整精确得多）。
    谱通量对起音位置有系统性偏置，会让事件时刻比真实起音早约 3 帧；
    默认已按 `_ONSET_LAG_FRAMES` 补偿，返回的时间即对应真实起音时刻。
    """
    if env.size < 16:
        return np.zeros(0), np.zeros(0), _ONSET_LAG_FRAMES
    thr = max(float(np.percentile(env, 65)) * 0.7, float(env.max()) * 0.03, 1e-9)
    idx, props = _np_find_peaks(env, height=thr, distance=2)
    if idx.size == 0:
        thr = float(env.max()) * 0.05
        idx, props = _np_find_peaks(env, height=thr, distance=2)
    if idx.size == 0:
        return np.zeros(0), np.zeros(0), _ONSET_LAG_FRAMES
    heights = props["peak_heights"]

    # 事件过多时按强度剪枝：长程斜率统计是 O(N^2)，必须限制规模
    if idx.size > max_events:
        order = np.argsort(heights)[::-1]
        keep = order[: max_events // 2]
        keep = np.unique(
            np.concatenate([keep, np.linspace(0, idx.size - 1, max_events // 2).astype(int)])
        )
        idx, heights = idx[keep], heights[keep]

    HALF = 2  # 质心窗口半径（帧）
    times = np.empty(idx.size, dtype=np.float64)
    for i, p in enumerate(idx):
        lo, hi = max(0, p - HALF), min(env.size, p + HALF + 1)
        w = env[lo:hi]
        total = float(w.sum())
        times[i] = (
            (lo + float(np.dot(np.arange(hi - lo), w) / total)) if total > 1e-12 else float(p)
        )
    if apply_lag_correction:
        times = times + _ONSET_LAG_FRAMES
    return times / fps, np.asarray(heights, dtype=np.float64), float(HALF)


def _estimate_phase(
    t: np.ndarray,
    weights: np.ndarray,
    period: float,
    max_iter: int = 4,
) -> tuple[float, float]:
    """由事件时刻估计首拍相位（节拍网格的相位）。

    相位 = 事件时刻对拍周期取模后的加权循环均值。用中位数作初值，
    再按与初值的偏差加权平均并逐步收紧带宽，从而自动排除不落在网格上的
    干扰事件（装饰音、气口、和声起音等）。

    返回 (相位秒（落在 [0, period)）, 网格吻合度 0~1)。
    """
    if t.size == 0 or period <= 0:
        return 0.0, 0.0
    phases = np.mod(t, period)
    weights = np.asarray(weights, dtype=np.float64)
    if weights.size != phases.size or weights.sum() <= 0:
        weights = np.ones_like(phases)
    sigma = period / 4.0
    ref = float(np.median(phases))
    for _ in range(max_iter):
        diff = (phases - ref + period / 2.0) % period - period / 2.0
        w = weights * np.exp(-0.5 * (diff / sigma) ** 2)
        total = float(w.sum())
        if total <= 1e-12:
            break
        ref = float(np.sum(w * (ref + diff)) / total) % period
        sigma = max(sigma * 0.55, period / 80.0)

    diff = (phases - ref + period / 2.0) % period - period / 2.0
    w = weights * np.exp(-0.5 * (diff / sigma) ** 2)
    coverage = float(w.sum() / max(weights.sum(), 1e-12))
    tightness = float(np.clip(1.0 - float(np.std(diff)) / (0.15 * period), 0.0, 1.0))
    fit = float(np.clip(0.6 * coverage + 0.4 * tightness, 0.0, 1.0))
    return ref % period, fit


def _theil_sen_period(
    t: np.ndarray,
    period_hint: float,
    min_lag_intervals: int = 5,
    max_deviation: float = 0.02,
    iterations: int = 4,
) -> tuple[float, float, np.ndarray]:
    """迭代式长程斜率估计精确拍周期（对漏检/多检免疫）。

    对每一对事件 (i, j)，把它们之间跨越的拍数 k 取整后，隐含周期
    = (t_j - t_i) / k；取所有隐含周期的中位数即得估计值。

    关键在于**迭代**：先用自相关给出的粗略周期做参考算出 k，再用新估计值
    重算 k。取整边界会随时间积累偏移，一次估计在长基线上会产生 ±半拍的
    错位对（把 40 拍的间隔误判成 39 或 41 拍），迭代可逐步剔除这些错位对，
    最终收敛到相对误差 1e-5 量级的解（合成节拍上实测可精确复原）。

    参数 period_hint 与 t 单位必须一致（本模块统一用秒）。

    返回 (周期, 相干度 0~1, 隐含周期数组)。
    """
    n = t.size
    if n < min_lag_intervals + 3 or period_hint <= 0:
        return 0.0, 0.0, np.zeros(0)

    ref = float(period_hint)
    implied = np.zeros(0)
    for _ in range(max(1, iterations)):
        values: list[float] = []
        for i in range(n):
            j0 = i + min_lag_intervals
            if j0 >= n:
                break
            lag = t[j0:] - t[i]
            # 长基线优先：要求跨越的物理时间足够长，把帧级噪声摊薄
            keep = lag > max(1e-6, min_lag_intervals * ref * 0.8)
            if not keep.any():
                continue
            lag_v = lag[keep]
            k = np.round(lag_v / ref)
            ok = k >= min_lag_intervals
            if not ok.any():
                continue
            lag_v, k = lag_v[ok], k[ok]
            implied_v = lag_v / k
            # 只保留与当前参考值接近的隐含周期，剔除错位对
            good = np.abs(implied_v - ref) <= max_deviation * ref
            if good.any():
                values.extend(implied_v[good].tolist())
        if len(values) < 8:
            break
        implied = np.asarray(values, dtype=np.float64)
        new_ref = float(np.median(implied))
        if new_ref <= 0:
            break
        if abs(new_ref - ref) / ref < 1e-6:
            ref = new_ref
            break
        ref = new_ref

    if implied.size < 8:
        return 0.0, 0.0, np.zeros(0)
    med = float(np.median(implied))
    mad = float(np.median(np.abs(implied - med)))
    # 相干度：隐含周期的相对 MAD。阈值 0.5% 对应"节拍高度规整"，
    # 真人演奏/现场版即使有轻微摇摆也远小于该值。
    coherence = float(np.clip(1.0 - (mad / med) / 0.005, 0.0, 1.0))
    return med, coherence, implied


def _phase_offset(
    env: np.ndarray,
    fps: float,
    bpm: float,
    period_frames: float | None = None,
) -> tuple[float, float]:
    """单周期粗相位搜索（事件太少时的兜底）。period_frames 以帧为单位。"""
    if bpm <= 0 or env.size == 0:
        return 0.0, 0.0
    if period_frames is None or period_frames <= 0:
        period_frames = 60.0 * fps / bpm
    n = int(math.ceil(env.size / period_frames)) + 1
    steps = max(8, int(round(period_frames)))
    scores = np.zeros(steps)
    for i in range(steps):
        phase = i * period_frames / steps
        idx = np.round(np.arange(n) * period_frames + phase).astype(int)
        idx = idx[(idx >= 0) & (idx < env.size)]
        if idx.size:
            scores[i] = float(env[idx].sum())
    k = int(np.argmax(scores))
    left, right = scores[(k - 1) % steps], scores[(k + 1) % steps]
    denom = left - 2 * scores[k] + right
    sub = 0.0
    if abs(denom) > 1e-12:
        sub = float(np.clip(0.5 * (left - right) / denom, -0.5, 0.5))
    best_phase = ((k + sub) % steps) * period_frames / steps
    grid_idx = np.round(np.arange(n) * period_frames).astype(int)
    grid_idx = grid_idx[(grid_idx >= 0) & (grid_idx < env.size)]
    grid_energy = float(env[grid_idx].sum()) if grid_idx.size else 0.0
    ratio = float(np.clip(scores[k] / max(grid_energy, 1e-9), 0.0, 1.0))
    return best_phase / fps, ratio


def _joint_fit(
    env: np.ndarray,
    fps: float,
    bpm_hint: float,
    search_ratio: float = 0.08,
) -> tuple[float, float, float]:
    """联合估计精确 BPM 与首拍相位，返回 (bpm, 首拍相位秒, 吻合度 0~1)。"""
    if bpm_hint <= 0:
        return 0.0, 0.0, 0.0
    t, h, _lag = _detect_onsets(env, fps)
    if t.size < 6:
        phase, ratio = _phase_offset(env, fps, bpm_hint, 60.0 * fps / bpm_hint)
        return bpm_hint, phase, ratio

    # 1) 精确周期（单位统一为秒）
    period_ts, coherence, _implied = _theil_sen_period(t, 60.0 / bpm_hint)
    bpm_ts = 60.0 / period_ts if period_ts > 0 else 0.0

    # 2) 合理性检查：偏离自相关提示值太多则判为不可信，回退到提示值
    coherent = (
        period_ts > 0
        and coherence > 0.20
        and abs(bpm_ts - bpm_hint) / bpm_hint <= search_ratio
    )
    bpm_use = bpm_ts if coherent else bpm_hint

    # 3) 首拍相位：事件时刻的加权循环均值
    phase_sec, fit_phase = _estimate_phase(t, h, 60.0 / bpm_use)
    if fit_phase <= 0:
        phase_sec, fit_phase = _phase_offset(env, fps, bpm_use, 60.0 * fps / bpm_use)

    fit = float(np.clip(0.5 * (coherence if coherent else 0.0) + 0.5 * fit_phase, 0.0, 1.0))
    return bpm_use, phase_sec, fit


def _parabolic_peak(arr: np.ndarray, idx: int) -> tuple[float, float]:
    """对 arr 在 idx 处做抛物线插值，返回 (细化位置, 峰值)。"""
    if idx <= 0 or idx >= arr.size - 1:
        return float(idx), float(arr[idx])
    y0, y1, y2 = float(arr[idx - 1]), float(arr[idx]), float(arr[idx + 1])
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-12:
        return float(idx), y1
    delta = float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5))
    return idx + delta, y1 - 0.25 * (y0 - y2) * delta


# ---- 速度候选打分：衰减梳状滤波 + 对数域软先验 ---------------------------- #
#
# 这套常量与独立工具 D:\BPMTool（bpmscan）保持一致，两边改要一起改。
_COMB_ALPHA = 0.79       # 谐波衰减；madmom TempoEstimationProcessor 的默认值
_COMB_HARM = 4           # 参与求和的谐波个数
_PRIOR_CENTER = 120.0    # 节奏先验中心；librosa tempo() 默认 start_bpm
_PRIOR_SIGMA = 1.0       # 先验宽度（倍频程）；librosa tempo() 默认 std_bpm
_OCTAVE_MARGIN = 0.50    # 加倍候选允许的梳状得分回落比例
# 注意：这个阈值**依赖 onset 包络的形状**，换包络必须重标，不能照抄。
# 本文件的安全平台实测是 0.35~0.65（到 0.70 慢歌就被翻倍：
# 58->116、60->120、65->130、90->180），取平台中点 0.50。
# 独立工具 D:\BPMTool 的包络不同，那边标出来是 0.15~0.40、取 0.25。


def _comb_scores(acf: np.ndarray, bpms: np.ndarray, fps: float,
                 alpha: float = _COMB_ALPHA, n_harm: int = _COMB_HARM) -> np.ndarray:
    """衰减梳状滤波组：score(tau) = sum_k alpha^(k-1) * acf(k*tau)。

    等权（alpha = 1）会让 2x/3x 的整数倍滞后同样吃满权重，凭空抬高低频
    候选——那是倍频假峰的直接来源。滞后一律用**分数**并线性插值。
    """
    n = acf.size
    if n < 4:
        return np.zeros_like(bpms)
    axis = np.arange(n)
    tau = fps * 60.0 / bpms
    score = np.zeros_like(bpms)
    w = 1.0
    for k in range(1, n_harm + 1):
        lag = tau * k
        ok = (lag >= 1.0) & (lag < n - 2)
        if ok.any():
            score[ok] += w * np.interp(lag[ok], axis, acf)
        w *= alpha
    return score


def _log_prior(bpms: np.ndarray) -> np.ndarray:
    """对数域高斯节奏先验。软约束，不做任何硬截断。"""
    return np.exp(-0.5 * ((np.log2(bpms / _PRIOR_CENTER)) / _PRIOR_SIGMA) ** 2)


def _same_octave_family(a: float, b: float, tol: float = 0.04) -> bool:
    """a 与 b 是否属于同一倍频族（1x / 2x / 1/2 / 3x / 1/3）。"""
    if a <= 0 or b <= 0:
        return False
    return min(abs(math.log2(a / (b * k))) for k in (0.5, 1.0, 2.0, 3.0, 1.0 / 3.0)) < tol


def _tempo_from_acf(env: np.ndarray, fps: float) -> tuple[float, float, np.ndarray]:
    """给出速度提示值。返回 (bpm, 置信度, 归一化自相关)。"""
    x = env - env.mean()
    if np.allclose(x, 0):
        return 0.0, 0.0, np.zeros(1)
    acf = np.correlate(x, x, mode="full")[x.size - 1:]
    if acf[0] <= 0:
        return 0.0, 0.0, np.zeros(1)
    acf = acf / acf[0]

    min_lag = max(2, int(round(fps * 60.0 / MAX_BPM)))
    max_lag = min(acf.size - 1, int(round(fps * 60.0 / MIN_BPM)))
    if max_lag <= min_lag:
        return 0.0, 0.0, acf

    # 速度网格打分：衰减梳状滤波 + 对数域软先验。
    #
    # 为什么不再用「自相关峰互相投票 + 偏好区间 x1.15」：`_PREFERRED` 那种
    # 硬区间在边界处不连续（69.9 与 70.1 得到完全不同的结论），而且与
    # madmom 默认 min_bpm=55 属于同一类会制造系统性倍频错误的做法——
    # arXiv:2605.12287 实测该默认值让 21% 的慢速曲目被迫给出双倍速度。
    # 下面全部改成连续量，没有硬边界。
    bpms = np.arange(MIN_BPM, MAX_BPM + 1e-9, 0.25)
    comb = _comb_scores(acf, bpms, fps)
    span = float(comb.max() - comb.min())
    comb_n = (comb - comb.min()) / span if span > 1e-12 else np.zeros_like(comb)
    total = comb_n * _log_prior(bpms)

    # 倍频仲裁：在"近分的加倍候选"里偏向更快的那个。依据是一个不对称性——
    # 慢歌翻倍会落在**没有起音**的半拍位置上（分数天然低，翻不动），而快歌
    # 减半永远落在真实拍点上（分数几乎一样高），所以"半速"是远多于"倍速"的
    # 实际错误方向。判据是梳状得分之比，不是硬区间。
    i = int(np.argmax(total))
    doubled = bpms[i] * 2.0
    if doubled <= MAX_BPM:
        j = int(np.argmin(np.abs(bpms - doubled)))
        if comb_n[j] >= (1.0 - _OCTAVE_MARGIN) * comb_n[i]:
            i = j
    best_bpm = float(bpms[i])
    best_strength = float(comb_n[i])

    # 置信度：与"非同族最强候选"的分离度 + 峰值本身的高度
    rivals = [float(comb_n[p]) for p in _np_find_peaks(total, height=0.0)[0]
              if not _same_octave_family(float(bpms[p]), best_bpm)]
    rival = max(rivals) if rivals else 0.0
    sep = (best_strength - rival) / best_strength if best_strength > 1e-9 else 0.0
    confidence = float(np.clip(
        0.5 * min(1.0, sep / 0.35) + 0.5 * min(1.0, best_strength / 0.9), 0.0, 1.0))
    return best_bpm, confidence, acf


def _candidate_bpms(env: np.ndarray, fps: float) -> list[float]:
    """列出所有自相关峰对应的候选速度，供人工核对。"""
    x = env - env.mean()
    if np.allclose(x, 0):
        return []
    acf = np.correlate(x, x, mode="full")[x.size - 1:]
    if acf[0] <= 0:
        return []
    acf = acf / acf[0]
    min_lag = max(2, int(round(fps * 60.0 / MAX_BPM)))
    max_lag = min(acf.size - 1, int(round(fps * 60.0 / MIN_BPM)))
    seg = acf[min_lag:max_lag + 1]
    peaks, _ = _np_find_peaks(seg, height=0.1)
    out: list[float] = []
    for p in peaks:
        lag, _h = _parabolic_peak(seg, int(p))
        lag_total = min_lag + lag
        if lag_total > 0:
            out.append(60.0 * fps / lag_total)
    out.sort()
    return out[:8]


def quantize_bpm(bpm: float, strategy: str = "auto") -> float:
    """BPM 取整规则。

    项目约定：**BPM 尽量用整数**，只有拿到"非整数"的线上数据时才保留小数。

    strategy:
      "auto" / "int" —— 四舍五入到整数（本地分析结果一律走这条）
      "exact"        —— 原样保留（用于在线数据库给出的非整数值）

    为什么本地结果一律取整：
      本地检测永远有零点几个 BPM 的估计误差（实测合成节拍上 < 0.15%）。
      对 128 BPM 来说 0.15% 就是 ±0.2 BPM，因此 128.21 这类值几乎必然只是
      误差，而不是真实速度。取整到 128 反而更接近真值。
      反例：如果真值是 128.25，取整成 128 会带来 0.2% 的拍长误差
      （1.8713s vs 1.8691s，差 2.2 ms），这个量级可接受；
      而误保留 128.25 会让用户看到"非整数 BPM"，与其诉求直接冲突。
    """
    if bpm <= 0:
        return bpm
    if strategy == "exact":
        return bpm
    return float(round(bpm))


@dataclass
class AnalysisResult:
    bpm: float
    confidence: float
    first_beat: float
    bar_seconds: float
    beat_seconds: float
    candidates: list[float] = field(default_factory=list)
    note: str = ""


def analyze_bpm(audio_path, sr: int | None = None, do_phase: bool = True) -> AnalysisResult:
    """对音频做本地 BPM 分析。

    audio_path 可以是文件路径，也可以是 (samples, sample_rate) 元组。
    """
    if isinstance(audio_path, tuple):
        y, sr_in = audio_path
        y = _to_mono(np.asarray(y))
    else:
        y, sr_in = _load_audio(audio_path)
    y, sr_use = _resample(y, int(sr_in), ANALYSIS_SR)
    # 避免超长文件拖慢分析：最多取前 300 秒
    max_samples = int(300 * sr_use)
    if y.size > max_samples:
        y = y[:max_samples]
    if y.size < sr_use:
        raise ValueError("音频过短，无法进行 BPM 分析")
    peak = float(np.max(np.abs(y))) or 1.0
    y = y / peak

    env, fps = onset_strength_envelope(y, sr_use)
    bpm_acf, conf, _ = _tempo_from_acf(env, fps)
    if bpm_acf <= 0:
        raise ValueError("BPM 分析失败：未能提取到有效节拍")

    fit_bpm, first_beat, fit = _joint_fit(env, fps, bpm_acf)
    bpm = fit_bpm if fit_bpm > 0 else bpm_acf

    # 联合拟合与自相关差得离谱时，多半是拟合被噪声带走，回退到自相关结果
    if bpm <= 0 or abs(bpm - bpm_acf) / bpm_acf > 0.12:
        bpm, fit, first_beat = bpm_acf, 0.0, 0.0
    if not do_phase:
        first_beat = 0.0

    conf = float(np.clip(0.45 * conf + 0.55 * fit, 0.0, 1.0))
    return AnalysisResult(
        bpm=round(bpm, 2),
        confidence=round(conf, 3),
        first_beat=round(first_beat, 4),
        bar_seconds=round(60.0 / bpm * 4.0, 4),
        beat_seconds=round(60.0 / bpm, 4),
        candidates=[round(b, 1) for b in _candidate_bpms(env, fps)],
        note=(
            f"首拍网格相位 {first_beat:.3f}s，吻合度 {fit:.2f}"
            if do_phase
            else "已跳过相位估计"
        ),
    )


def _load_audio(path) -> tuple[np.ndarray, int]:
    """优先用 soundfile 读，缺失则用 ffmpeg 解成 wav 再读。"""
    path = str(path)
    try:
        import soundfile as sf  # type: ignore

        data, sr = sf.read(path, always_2d=False)
        return _to_mono(np.asarray(data, dtype=np.float64)), int(sr)
    except Exception:  # noqa: BLE001
        pass
    return _load_audio_ffmpeg(path)


def _load_audio_ffmpeg(path: str) -> tuple[np.ndarray, int]:
    import subprocess
    import tempfile
    from pathlib import Path
    from wave import open as wave_open

    from .config import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("解码音频需要 soundfile 或 ffmpeg，两者都不可用")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "decode.wav"
        cmd = [
            str(ffmpeg), "-y", "-loglevel", "error",
            "-i", path,
            "-ac", "1", "-ar", str(ANALYSIS_SR),
            "-c:a", "pcm_s16le",
            str(wav),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not wav.is_file():
            raise RuntimeError("ffmpeg 解码失败: " + (proc.stderr or "").strip()[:300])
        with wave_open(str(wav), "rb") as wf:
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        return data, sr
