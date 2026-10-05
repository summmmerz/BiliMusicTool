"""BPM 检测算法自检：用合成节拍音频验证准确度，并验证空隙处理链路。

用法:
    python selftest.py            # 只测 BPM 算法（不需要 ffmpeg）
    python selftest.py --full     # 额外验证 ffmpeg 补空隙链路（需要 ffmpeg）
"""
from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bilimusic.bpm_detector import analyze_bpm  # noqa: E402
from bilimusic.config import SAMPLE_RATE, find_ffmpeg  # noqa: E402

C_DIM = "\033[90m"
C_END = "\033[0m"


def make_click_track(bpm: float, seconds: float = 40.0, sr: int = SAMPLE_RATE, seed: int = 0) -> np.ndarray:
    """合成一首"歌曲"：稳定的 kick（每拍）+ snare（2/4 拍）+ 轻噪声，便于节拍检测。"""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    y = rng.normal(0, 0.002, n)  # 底噪
    beat = 60.0 / bpm
    t = 0.0
    idx = 0
    while t < seconds:
        start = int(t * sr)
        # kick：55Hz 衰减正弦
        dur = int(0.18 * sr)
        tt = np.arange(dur) / sr
        kick = 0.9 * np.sin(2 * math.pi * 55 * tt) * np.exp(-tt * 22)
        end = min(n, start + dur)
        if end > start:
            y[start:end] += kick[: end - start]
        # snare（第 3 拍，即 2/4 的"2"）
        if idx % 4 == 2:
            dur2 = int(0.12 * sr)
            tt2 = np.arange(dur2) / sr
            snare = 0.55 * rng.normal(0, 1, dur2) * np.exp(-tt2 * 36)
            end2 = min(n, start + dur2)
            if end2 > start:
                y[start:end2] += snare[: end2 - start]
        t += beat
        idx += 1
    # 加一点和声内容，更接近真实音乐
    tt = np.arange(n) / sr
    y += 0.05 * np.sin(2 * math.pi * 440 * tt) * (1 + 0.5 * np.sin(2 * math.pi * (bpm / 60) * tt))
    y /= np.max(np.abs(y)) or 1.0
    return y.astype(np.float32)


def test_bpm_accuracy(cases: list[tuple[float, float]]) -> int:
    print("=" * 70)
    print(" BPM 检测准确度自检（合成节拍音频）")
    print("=" * 70)
    print(f"{'真实BPM':>8} {'检测BPM':>9} {'误差%':>8} {'置信度':>7} {'结论':>6}")
    print("-" * 70)
    failures = 0
    for bpm, seconds in cases:
        y = make_click_track(bpm, seconds=seconds, seed=int(bpm))
        try:
            res = analyze_bpm((y.astype(np.float64), SAMPLE_RATE))
        except Exception as exc:  # noqa: BLE001
            print(f"{bpm:>8.1f} {'-':>9} {'-':>8} {'-':>7}  异常: {exc}")
            failures += 1
            continue
        # 允许倍频/半频判定为"部分正确"（音乐上常见歧义）
        ratio = res.bpm / bpm
        rel = abs(ratio - 1.0) * 100
        if rel <= 2.0:
            verdict = "准确"
        elif min(abs(ratio - 2.0), abs(ratio - 0.5)) < 0.03:
            verdict = "倍频"
            failures += 1
        else:
            verdict = "偏差"
            failures += 1
        print(f"{bpm:>8.1f} {res.bpm:>9.2f} {rel:>8.2f} {res.confidence:>7.2f} {verdict:>6}")
    print("-" * 70)
    print(f"失败（非严格准确）: {failures}/{len(cases)}")
    return failures


def test_phase_detection() -> int:
    """验证首拍定位：把音频整体后移已知偏移，检测到的相位应与之吻合（模拍周期）。"""
    print("\n" + "=" * 70)
    print(" 首拍相位定位自检（相位按拍周期取模比较）")
    print("=" * 70)
    print(f"{'BPM':>7} {'真实偏移':>9} {'检测相位':>9} {'误差(s)':>9} {'误差/拍':>8} {'结论':>6}")
    print("-" * 70)
    fails = 0
    cases = [(128.0, 0.137), (120.0, 0.5), (90.0, 0.0), (174.0, 0.31)]
    for bpm, offset in cases:
        y = make_click_track(bpm, seconds=30.0, seed=int(bpm))
        shifted = np.concatenate([np.zeros(int(offset * SAMPLE_RATE), dtype=np.float32), y])
        res = analyze_bpm((shifted.astype(np.float64), SAMPLE_RATE))
        beat = 60.0 / bpm
        true_phase = offset % beat
        raw = abs(res.first_beat - true_phase)
        err = min(raw % beat, beat - (raw % beat))
        bpm_ok = abs(res.bpm - bpm) / bpm < 0.01
        okk = err < 0.02 and bpm_ok
        if not okk:
            fails += 1
        print(f"{bpm:>7.1f} {offset:>9.4f} {res.first_beat:>9.4f} {err:>9.4f} "
              f"{err / beat * 100:>7.1f}% {'通过' if okk else '未通过':>6}")
        if not okk:
            print(f"        {C_DIM}检测BPM={res.bpm:g} (真值 {bpm:g})  真实相位={true_phase:.4f}  "
                  f"每拍={beat:.4f}  原始差={raw:.4f}{C_END}")
    print("-" * 70)
    return fails


def test_padding_pipeline() -> int:
    """验证 ffmpeg 补空隙链路：补入时长与理论值一致。"""
    print("\n" + "=" * 70)
    print(" 补空隙处理链路自检（需要 ffmpeg）")
    print("=" * 70)
    if not find_ffmpeg():
        print("未找到 ffmpeg，跳过")
        return 0
    from bilimusic.audio_processor import gap_for_bpm, probe_duration, process_audio
    import wave

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        wav = td_path / "in.wav"
        y = make_click_track(150.0, seconds=10.0, seed=3)
        pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())
        src_dur = probe_duration(wav)
        bpm = 150.0
        gap = gap_for_bpm(bpm, 4)
        out = td_path / "out.mp3"
        res = process_audio(wav, out, gap_seconds=gap, title="selftest", bpm=bpm)
        expected = (src_dur or 0) + gap
        got = res.total_duration or 0
        delta = abs(got - expected)
        print(f"源时长 {src_dur:.3f}s + 1小节空隙 {gap:.3f}s = 期望 {expected:.3f}s")
        print(f"实际输出 {got:.3f}s   偏差 {delta:.3f}s   （MP3 编码填充约 0.02~0.05s 属正常）")
        print(f"输出文件大小 {out.stat().st_size / 1024:.0f} KB")
        okk = delta < 0.12
        print("结论:", "通过" if okk else "未通过")
        return 0 if okk else 1


def test_bpm_quantization() -> int:
    """验证 BPM 取整规则：本地结果一律取整，仅线上非整数保留小数。"""
    print("\n" + "=" * 70)
    print(" BPM 取整规则自检")
    print("=" * 70)
    from bilimusic.bpm_detector import quantize_bpm

    # (输入, 期望的 auto 结果, 期望的 exact 结果, 说明)
    cases = [
        (173.97, 174.0, 173.97, "本地估计误差 -> 取整"),
        (153.99, 154.0, 153.99, "本地估计误差 -> 取整"),
        (128.21, 128.0, 128.21, "本地估计误差 -> 取整（不能变成 128.25）"),
        (128.0, 128.0, 128.0, "本来就是整数"),
        (90.5, 90.0, 90.5, "本地半整数也取整"),
        (100.25, 100.0, 100.25, "本地四分位也取整"),
        (87.4, 87.0, 87.4, "本地结果取整"),
        (0.0, 0.0, 0.0, "无效值原样返回"),
    ]
    fails = 0
    print(f"{'输入':>9} {'auto(本地)':>11} {'exact(线上)':>12}  说明")
    print("-" * 70)
    for raw, want_auto, want_exact, note in cases:
        got_auto = quantize_bpm(raw, "auto")
        got_exact = quantize_bpm(raw, "exact")
        ok = abs(got_auto - want_auto) < 1e-9 and abs(got_exact - want_exact) < 1e-9
        if not ok:
            fails += 1
        print(f"{raw:>9.2f} {got_auto:>11} {got_exact:>12}  "
              f"{'OK  ' if ok else 'FAIL'} {note}")
    print("-" * 70)
    # 关键约束：auto 结果必须是整数
    non_int = [v for v in (quantize_bpm(r, "auto") for r, *_ in cases)
               if abs(v - round(v)) > 1e-9]
    if non_int:
        print(f"!! auto 结果出现非整数: {non_int}")
        fails += 1
    else:
        print("约束检查通过：auto 模式输出全部为整数")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="BiliMusicTool 自检")
    ap.add_argument("--full", action="store_true", help="额外验证 ffmpeg 补空隙链路")
    args = ap.parse_args()

    # 60/75 与 180/196 是刻意加进来的**边界外**用例：
    # 早先的倍频仲裁用 `_PREFERRED = (70, 180)` 硬区间，这四点正好落在
    # 区间外/边界上，而原来的测试集只有 90~174、全在区间内部，所以那个
    # 系统性倍频缺陷一直测不出来。保留这批用例，防止再退回去。
    cases = [
        (60.0, 40.0),
        (75.0, 40.0),
        (90.0, 40.0),
        (100.0, 40.0),
        (120.0, 40.0),
        (128.0, 40.0),
        (150.0, 40.0),
        (174.0, 40.0),
        (180.0, 40.0),
        (196.0, 40.0),
    ]
    fails = test_bpm_accuracy(cases)
    fails += test_phase_detection()
    fails += test_bpm_quantization()
    if args.full:
        fails += test_padding_pipeline()

    print("\n" + "=" * 70)
    print(f" 自检结束：{'全部通过' if fails == 0 else f'{fails} 项未通过'}")
    print("=" * 70)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
