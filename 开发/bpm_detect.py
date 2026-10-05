# -*- coding: utf-8 -*-
"""离线 BPM 检测（纯 numpy，零第三方依赖）。

背景：公开 BPM 库已全部不可用（getonboard 关停、songbpm 只认拉丁字母 slug、
GetSongBPM 要付费 key、AcousticBrainz 下线、MusicBrainz 压根没有 BPM 字段），
所以只能自己从音频里算。

算法（三步，都不需要任何训练/模型文件）：
  1. ffmpeg 解码成 22.05 kHz 单声道 → 短时傅里叶 → **谱通量** onset 包络；
  2. **自相关 + 谐波求和**估计周期（对 2/3/4 倍滞后加权），
     再乘一个对数正态节奏先验（中心 120 BPM）压制倍频/半频；
  3. 倍频校正 + 相位折叠求首拍位置，输出最优 BPM、首拍时间与前 5 个候选。

已知局限：自相关本质上分不清"一拍"和"半拍"（两者都是完美周期）。
最终拍级由 PREF_MIN~PREF_MAX 区间先验决定，所以 87/174 这类
二义性节奏只能给一个"更可能"的答案，输出里保留了候选供人工复核。

用法：
    python bpm_detect.py 音频文件 [更多文件...]
    python bpm_detect.py --selftest        # 用合成节拍验证算法本身
    python bpm_detect.py --selftest --on 千本桜.mp3   # 自检 + 实测
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

SR = 22050           # 解码采样率
N_FFT = 2048
HOP = 256
FPS = SR / HOP       # ≈ 86.13 帧/秒，决定 BPM 的量化精度
BPM_MIN, BPM_MAX = 40.0, 220.0

# 节奏先验：人类音乐绝大部分落在这里面，用它压倍频最省事
PRIOR_CENTER, PRIOR_SIGMA = 120.0, 0.75   # 单位是"倍频程"（log2）
PREF_MIN, PREF_MAX = 95.0, 190.0          # 认为"这就是拍子"的区间
OCTAVE_TOL = 0.6                          # 翻倍频时允许的分数回落比例

_SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 解码

def find_ffmpeg():
    """优先用项目自带的 ffmpeg（BiliMusicTool 的 tools\\ffmpeg），其次 PATH。"""
    for cand in (
        _SCRIPT_DIR.parent / "program" / "tools" / "ffmpeg" / "ffmpeg.exe",
        _SCRIPT_DIR.parent / "tools" / "ffmpeg" / "ffmpeg.exe",
    ):
        if cand.exists():
            return str(cand)
    return shutil.which("ffmpeg")


def decode(path, ffmpeg):
    """解码成单声道 22.05 kHz float32。中间 WAV 放临时目录，用完即删。"""
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg：请把 ffmpeg.exe 放进 tools\\ffmpeg，或加进 PATH")
    tmp = tempfile.mkdtemp(prefix="bpm_")
    try:
        out = os.path.join(tmp, "a.wav")
        cmd = [ffmpeg, "-v", "error", "-y", "-i", str(path),
               "-ac", "1", "-ar", str(SR), "-f", "wav", out]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or not os.path.exists(out):
            err = (r.stderr or b"").decode("utf-8", "ignore").strip()[-600:]
            raise RuntimeError("ffmpeg 解码失败（退出码 %s）：%s" % (r.returncode, err))
        with wave.open(out, "rb") as w:
            n, sw = w.getnframes(), w.getsampwidth()
            raw = w.readframes(n)
        if sw != 2:
            raise RuntimeError("ffmpeg 输出的不是 16-bit PCM（sampwidth=%s）" % sw)
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- onset

def onset_envelope(x):
    """谱通量 onset 包络。返回长度 = 帧数 的 1-D float32。"""
    if len(x) < N_FFT * 2:
        raise RuntimeError("音频太短（不足 %d 个采样点）" % (N_FFT * 2))
    n_frames = 1 + (len(x) - N_FFT) // HOP
    win = np.hanning(N_FFT).astype(np.float32)
    flux = np.empty(n_frames, dtype=np.float32)
    prev = None
    BLK = 2048                      # 分块做 FFT，避免一次性构造 n_frames×N_FFT 的索引矩阵
    base = np.arange(N_FFT)
    for s in range(0, n_frames, BLK):
        nb = min(BLK, n_frames - s)
        idx = (s * HOP + np.arange(nb) * HOP)[:, None] + base[None, :]
        mag = np.abs(np.fft.rfft(x[idx] * win, axis=1))
        mag = np.log1p(mag * 20.0)  # 对数压缩：弱拍也保留一点点能量
        if prev is None:
            d = np.empty_like(mag)
            d[0] = 0.0
            d[1:] = mag[1:] - mag[:-1]
        else:
            d = mag - np.vstack((prev, mag[:-1]))
        flux[s:s + nb] = np.maximum(d, 0.0).sum(axis=1)
        prev = mag[-1]
    # 减掉 ~1 秒的滑动均值：去掉整段响度漂移，只留"突变"
    k = int(round(FPS))
    smooth = np.convolve(flux, np.ones(k, np.float32) / k, mode="same")
    e = np.maximum(flux - smooth, 0.0)
    sd = float(e.std())
    return e / sd if sd > 1e-9 else e


def half_wave(e):
    return e - e.mean()


# ---------------------------------------------------------------- 两路周期估计

def tempo_candidates():
    return np.arange(BPM_MIN, BPM_MAX + 1e-9, 0.25)


def score_acf(e, bpms):
    """自相关 + 谐波求和：真周期在 1/2/3/4 倍滞后处都该有峰。"""
    n = len(e)
    ec = e - e.mean()
    f = np.fft.rfft(ec, 2 * n)
    acf = np.fft.irfft(f * np.conj(f))[:n].astype(np.float64)
    if acf[0] <= 1e-12:
        return np.zeros_like(bpms)
    acf /= acf[0]
    lags = FPS * 60.0 / bpms
    score = np.zeros_like(bpms)
    axis = np.arange(n)
    for k, w in ((1, 1.0), (2, 0.6), (3, 0.4), (4, 0.25)):
        li = lags * k                      # 分数滞后：必须插值，取整会把 154 BPM 对到 77 上
        ok = (li > 1.0) & (li < n - 2)
        if ok.any():
            score[ok] += w * np.interp(li[ok], axis, acf)
    return score


def beat_phase(e, bpm):
    """给定周期，求**首拍位置**（秒）与拍点清晰度。

    相位折叠只用来定"相位"，不用来定"周期"：折叠格数 = 周期长度，
    格数越多最大值天然越大（z 分数仍随 P 增长），拿它比不同 BPM 会
    系统性偏袒低 BPM —— 实测直接把 154 判成 77。周期交给自相关定。
    """
    n = len(e)
    P = int(round(FPS * 60.0 / bpm))
    if P < 2 or P >= n:
        return 0.0, 0.0
    acc = np.bincount(np.arange(n) % P, weights=e, minlength=P)
    ph = int(np.argmax(acc))
    sd = float(acc.std())
    strength = (float(acc.max()) - float(acc.mean())) / sd if sd > 1e-12 else 0.0
    return ph / FPS, strength


def norm01(v):
    lo, hi = float(v.min()), float(v.max())
    return (v - lo) / (hi - lo) if hi - lo > 1e-12 else np.zeros_like(v)


def tempo_prior(bpms):
    return np.exp(-0.5 * (np.log2(bpms / PRIOR_CENTER) / PRIOR_SIGMA) ** 2)


def estimate_bpm(x, topn=5):
    e = onset_envelope(x)
    bpms = tempo_candidates()
    a = norm01(score_acf(e, bpms))
    total = a * tempo_prior(bpms)

    # 倍频校正：自相关分不清"一拍"和"半拍"——两者都是完美周期，峰一样高。
    # 这是节拍检测的固有歧义，只能靠"人一般把哪个当拍"来定，也就是区间先验。
    # 低于下限就试着往上翻一倍，高于上限就试着往下减半，
    # 但要求那一档的分数别掉太多（OCTAVE_TOL），否则宁可报原值。
    i = int(np.argmax(total))
    if bpms[i] < PREF_MIN:
        j = int(np.argmin(np.abs(bpms - bpms[i] * 2.0)))
        if bpms[j] <= PREF_MAX and total[j] >= OCTAVE_TOL * total[i]:
            i = j
    elif bpms[i] > PREF_MAX:
        j = int(np.argmin(np.abs(bpms - bpms[i] / 2.0)))
        if bpms[j] >= PREF_MIN and total[j] >= OCTAVE_TOL * total[i]:
            i = j

    # 取局部极大值再排序，避免同一个峰被重复列出
    pk = np.where((total[1:-1] > total[:-2]) & (total[1:-1] >= total[2:]))[0] + 1
    if pk.size == 0:
        pk = np.array([i])
    if i not in pk:
        pk = np.append(pk, i)
    order = pk[np.argsort(total[pk])[::-1]][:topn]

    bpm = round(float(bpms[i]), 2)
    ph, strength = beat_phase(e, bpm)
    cands = [{"bpm": round(float(bpms[k]), 2),
              "score": round(float(total[k]), 4),
              "acf": round(float(a[k]), 4)} for k in order]
    meta = {"bpm": bpm, "phase_sec": round(ph, 3), "phase_strength": round(strength, 2)}
    return cands, e, meta


# ---------------------------------------------------------------- 自检

def click_track(bpm, seconds=40.0, seed=1):
    """合成节拍轨：底噪 + 每拍一个短脉冲，4/4 小节首拍加重。
    这是**有确切真值**的输入，用来证明算法本身没写错。"""
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, 0.0025, int(seconds * SR)).astype(np.float32)
    period = int(round(60.0 / bpm * SR))
    click = (np.hanning(96) ** 2).astype(np.float32) * 0.9
    click = np.concatenate([click, np.zeros(1600 - 96, np.float32)])
    for i, pos in enumerate(range(0, len(x) - click.size, period)):
        amp = 1.0 if i % 4 == 0 else (0.55 if i % 2 == 0 else 0.4)
        x[pos:pos + click.size] += click * amp
    return x


def selftest():
    print("== 自检（合成节拍，真值已知）==")
    bad = 0
    for bpm in (80.0, 100.0, 128.0, 154.0, 175.0):
        x = click_track(bpm)
        cands, _, meta = estimate_bpm(x)
        got = meta["bpm"]
        # 允许 2 倍频/半频之外的小误差；只看是否命中真值或它的倍频
        ratio = got / bpm
        ok = any(abs(ratio - r) < 0.02 for r in (0.5, 1.0, 2.0))
        exact = abs(got - bpm) < 1.0
        bad += 0 if exact else 1
        print("  真值 %6.1f  ->  检测 %6.2f  %s   候选: %s"
              % (bpm, got, "OK" if exact else ("倍频 OK" if ok else "MISS"),
                 [c["bpm"] for c in cands[:3]]))
    print("  逐条精确命中 %d/5" % (5 - bad))
    return bad


# ---------------------------------------------------------------- 主流程

def analyze(path, ffmpeg):
    x = decode(path, ffmpeg)
    cands, e, meta = estimate_bpm(x)
    dur = len(x) / float(SR)
    print("\n文件: %s" % os.path.basename(path))
    print("  时长 %.1f 秒   包络 %d 帧 (%.1f fps)" % (dur, len(e), FPS))
    print("  >> BPM = %.1f   首拍 ≈ %.3f 秒   拍点清晰度 %.2f"
          % (meta["bpm"], meta["phase_sec"], meta["phase_strength"]))
    print("  候选（BPM / 总分 / 自相关）:")
    for c in cands:
        print("     %7.2f   %.4f   %.4f" % (c["bpm"], c["score"], c["acf"]))
    return cands


def main():
    ap = argparse.ArgumentParser(description="离线 BPM 检测（纯 numpy）")
    ap.add_argument("files", nargs="*", help="音频文件（mp3/m4a/flac/wav…）")
    ap.add_argument("--selftest", action="store_true", help="用合成节拍验证算法")
    ap.add_argument("--on", action="append", default=[], help="自检之外再测一个真实文件")
    args = ap.parse_args()

    if args.selftest:
        selftest()

    targets = list(args.files) + list(args.on)
    if not targets and not args.selftest:
        ap.print_help()
        return 2

    if targets:
        ffmpeg = find_ffmpeg()
        print("\nffmpeg: %s" % (ffmpeg or "未找到"))
        for t in targets:
            if not os.path.exists(t):
                print("\n文件不存在: %s" % t)
                continue
            try:
                analyze(t, ffmpeg)
            except Exception as exc:                       # noqa: BLE001
                print("\n处理失败 %s: %s" % (t, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
