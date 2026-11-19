# -*- coding: utf-8 -*-
"""番茄钟：专注 / 短休息 / 长休息 + 白噪音。

运行方式：
    python pomodoro.py

白噪音使用随机数实时合成并写入临时 wav 文件（白噪音 / 棕色噪音 / 雨声），
通过 Windows 自带的 winsound 循环播放，不需要安装任何第三方库。
"""

from __future__ import annotations

import array
import math
import os
import random
import tempfile
import threading
import time
import tkinter as tk
import wave

try:
    import winsound
except ImportError:  # 非 Windows 平台
    winsound = None

SR = 22050                 # 采样率
NOISE_SECONDS = 8          # 循环片段长度（秒）
FONT = "Microsoft YaHei UI"

BG = "#1b1d23"
CARD = "#252830"
CHIP = "#333845"
FG = "#eceef4"
MUTED = "#8b90a0"
ACCENT = {"work": "#ff6b6b", "short": "#4ecdc4", "long": "#6b8cff"}
MODE_NAMES = {"work": "专注时间", "short": "短休息", "long": "长休息"}
LEVEL_GAIN = {"低": 0.05, "中": 0.12, "高": 0.25}
KIND_SLUG = {"白噪音": "white", "棕色噪音": "brown", "雨声": "rain"}


# --------------------------------------------------------------------------- #
# 音频
# --------------------------------------------------------------------------- #
class NoiseEngine:
    """合成白噪音 wav 文件，并用 winsound 循环播放。"""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.enabled = False
        self.kind = "白噪音"
        self.level = "中"
        self._token = 0
        self._lock = threading.Lock()

    # -- 文件生成 -----------------------------------------------------------
    def _path(self, kind: str, level: str) -> str:
        return os.path.join(self.folder, f"noise_{KIND_SLUG[kind]}_{level}.wav")

    def build(self, kind: str, level: str) -> str:
        path = self._path(kind, level)
        with self._lock:
            if os.path.exists(path):
                return path
            n = SR * NOISE_SECONDS
            if kind == "白噪音":
                samples = [random.uniform(-1.0, 1.0) for _ in range(n)]
            elif kind == "棕色噪音":
                samples = []
                v = 0.0
                for _ in range(n):
                    v = (v + random.uniform(-1.0, 1.0) * 0.02) * 0.998
                    samples.append(v)
            else:  # 雨声：低通白噪音 + 随机雨滴
                samples = []
                lp = 0.0
                for _ in range(n):
                    lp += (random.uniform(-1.0, 1.0) - lp) * 0.12
                    samples.append(lp)
                for _ in range(n // 60):
                    start = random.randrange(n - 400)
                    amp = random.uniform(0.3, 1.0)
                    for i in range(300):
                        samples[start + i] += (
                            amp * math.exp(-i / 60.0) * random.uniform(-1.0, 1.0) * 0.6
                        )

            peak = max(1e-9, max(abs(s) for s in samples))
            gain = LEVEL_GAIN[level]
            fade = min(2000, n // 2)
            data = array.array("h")
            for i, s in enumerate(samples):
                env = 1.0
                if i < fade:
                    env = i / fade
                elif i > n - fade:
                    env = (n - i) / fade
                val = s / peak * gain * env * 32767.0
                data.append(max(-32768, min(32767, int(val))))

            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SR)
                wf.writeframes(data.tobytes())
            return path

    # -- 播放控制 -----------------------------------------------------------
    def enable(self, kind: str | None = None, level: str | None = None) -> None:
        if winsound is None:
            return
        if kind:
            self.kind = kind
        if level:
            self.level = level
        self.enabled = True
        self._token += 1
        token = self._token
        threading.Thread(target=self._play_async, args=(token,), daemon=True).start()

    def _play_async(self, token: int) -> None:
        path = self.build(self.kind, self.level)
        if token != self._token or not self.enabled:
            return
        try:
            winsound.PlaySound(
                path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP
            )
        except RuntimeError:
            pass

    def _stop_sound(self) -> None:
        if winsound is None:
            return
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except RuntimeError:
            pass

    def pause(self) -> None:
        self._token += 1
        self._stop_sound()

    def resume(self) -> None:
        if self.enabled:
            self.enable()

    def disable(self) -> None:
        self.enabled = False
        self._token += 1
        self._stop_sound()


def make_chime(folder: str) -> str:
    """生成一段三音提示音，返回 wav 路径。"""
    path = os.path.join(folder, "chime.wav")
    if os.path.exists(path):
        return path
    data = array.array("h")
    for freq, dur in ((880.0, 0.16), (1174.66, 0.16), (1567.98, 0.4)):
        n = int(SR * dur)
        for i in range(n):
            env = min(1.0, i / 200.0) * (1.0 - i / n) ** 1.5
            data.append(int(math.sin(2 * math.pi * freq * i / SR) * env * 0.35 * 32767))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(data.tobytes())
    return path


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
class PomodoroApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("番茄钟")
        root.configure(bg=BG)
        root.geometry("420x640")
        root.resizable(False, False)

        # 状态
        self.durations = {"work": 25, "short": 5, "long": 15}
        self.mode = "work"
        self.phase_total = float(self.durations["work"] * 60)
        self.remaining = self.phase_total
        self.running = False
        self.deadline = 0.0
        self.pomodoros = 0
        self._tick_id: str | None = None
        self._sync_guard = False

        # 音频
        self.folder = os.path.join(tempfile.gettempdir(), "pomodoro_clock")
        os.makedirs(self.folder, exist_ok=True)
        self.noise = NoiseEngine(self.folder)
        self.chime_path = make_chime(self.folder)
        self.noise_kind = tk.StringVar(value="白噪音")
        self.noise_level = tk.StringVar(value="中")

        self._build_ui()
        self._render()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- 布局 ---------------------------------------------------------------
    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=24, pady=(18, 0))
        tk.Label(header, text="番茄钟", bg=BG, fg=FG, font=(FONT, 16, "bold")).pack(side="left")
        self.session_label = tk.Label(
            header, text="已完成 0 个", bg=BG, fg=MUTED, font=(FONT, 10)
        )
        self.session_label.pack(side="right")

        # 圆环 + 时间
        self.canvas = tk.Canvas(self.root, width=300, height=300, bg=BG, highlightthickness=0)
        self.canvas.pack(pady=(2, 0))
        self.canvas.create_arc(
            24, 24, 276, 276, start=90, extent=-359.9,
            style="arc", outline="#2c3038", width=12,
        )
        self.arc = self.canvas.create_arc(
            24, 24, 276, 276, start=90, extent=-359.9,
            style="arc", outline=ACCENT["work"], width=12,
        )
        self.mode_text = self.canvas.create_text(
            150, 112, text="专注时间", fill=ACCENT["work"], font=(FONT, 11)
        )
        self.time_text = self.canvas.create_text(
            150, 152, text="25:00", fill=FG, font=("Consolas", 44, "bold")
        )
        self.round_text = self.canvas.create_text(
            150, 198, text="第 1 个番茄", fill=MUTED, font=(FONT, 10)
        )

        # 控制按钮
        controls = tk.Frame(self.root, bg=BG)
        controls.pack(fill="x", padx=24, pady=(0, 8))
        self.start_btn = tk.Button(
            controls, text="开始", command=self._toggle,
            bg=ACCENT["work"], fg="#15171c", font=(FONT, 12, "bold"),
            relief="flat", bd=0, cursor="hand2", height=2,
            activeforeground="#15171c",
        )
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 8))
        self.reset_btn = tk.Button(
            controls, text="重置", command=self._reset,
            bg=CHIP, fg=FG, font=(FONT, 11), relief="flat", bd=0,
            cursor="hand2", height=2, activebackground="#3d424f", activeforeground=FG,
        )
        self.reset_btn.pack(side="left", padx=(0, 8))
        self.skip_btn = tk.Button(
            controls, text="跳过", command=self._skip,
            bg=CHIP, fg=FG, font=(FONT, 11), relief="flat", bd=0,
            cursor="hand2", height=2, activebackground="#3d424f", activeforeground=FG,
        )
        self.skip_btn.pack(side="left")

        # 白噪音卡片
        noise_card = tk.Frame(self.root, bg=CARD)
        noise_card.pack(fill="x", padx=24, pady=(0, 8))
        top = tk.Frame(noise_card, bg=CARD)
        top.pack(fill="x", padx=14, pady=(10, 2))
        tk.Label(top, text="白噪音", bg=CARD, fg=FG, font=(FONT, 11, "bold")).pack(side="left")
        self.noise_btn = tk.Button(
            top, text="开启", command=self._toggle_noise,
            bg=CHIP, fg=FG, font=(FONT, 10), relief="flat", bd=0,
            cursor="hand2", padx=12, activebackground="#3d424f", activeforeground=FG,
        )
        self.noise_btn.pack(side="right")

        kind_row = tk.Frame(noise_card, bg=CARD)
        kind_row.pack(fill="x", padx=14, pady=(0, 2))
        tk.Label(kind_row, text="音色", bg=CARD, fg=MUTED, font=(FONT, 10), width=4, anchor="w").pack(side="left")
        for kind in ("白噪音", "棕色噪音", "雨声"):
            self._chip(kind_row, kind, self.noise_kind, self._on_noise_change)

        level_row = tk.Frame(noise_card, bg=CARD)
        level_row.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(level_row, text="音量", bg=CARD, fg=MUTED, font=(FONT, 10), width=4, anchor="w").pack(side="left")
        for level in ("低", "中", "高"):
            self._chip(level_row, level, self.noise_level, self._on_noise_change)

        if winsound is None:
            self.noise_btn.config(text="不可用", state="disabled")

        # 设置卡片
        settings = tk.Frame(self.root, bg=CARD)
        settings.pack(fill="x", padx=24, pady=(0, 14))
        dur_row = tk.Frame(settings, bg=CARD)
        dur_row.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(dur_row, text="时长(分钟)", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
        self.dur_vars: dict[str, tk.IntVar] = {}
        for key, label in (("long", "长休"), ("short", "短休"), ("work", "专注")):
            box = tk.Frame(dur_row, bg=CARD)
            box.pack(side="right", padx=(12, 0))
            tk.Label(box, text=label, bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
            var = tk.IntVar(value=self.durations[key])
            self.dur_vars[key] = var
            spin = tk.Spinbox(
                box, from_=1, to=180, width=3, textvariable=var,
                bg=CHIP, fg=FG, buttonbackground=CHIP, relief="flat",
                font=(FONT, 10), justify="center", insertbackground=FG,
            )
            spin.pack(side="left", padx=(4, 0))
            var.trace_add("write", lambda *_, k=key: self._on_duration_change(k))
        self.auto_next = tk.BooleanVar(value=True)
        tk.Checkbutton(
            settings, text="自动开始下一阶段", variable=self.auto_next,
            bg=CARD, fg=MUTED, selectcolor=CHIP, activebackground=CARD,
            activeforeground=FG, font=(FONT, 10), bd=0, highlightthickness=0,
        ).pack(anchor="w", padx=12, pady=(0, 10))

    def _chip(self, parent: tk.Frame, text: str, var: tk.StringVar, cmd) -> None:
        tk.Radiobutton(
            parent, text=text, variable=var, value=text, command=cmd,
            indicatoron=False, bg=CHIP, fg=MUTED, selectcolor="#3f4a5e",
            activebackground="#3d424f", activeforeground=FG,
            font=(FONT, 10), bd=0, relief="flat", padx=8, pady=2,
            highlightthickness=0, cursor="hand2",
        ).pack(side="left", padx=(6, 0))

    # -- 计时 ---------------------------------------------------------------
    def _toggle(self) -> None:
        if self.running:
            self._pause()
        else:
            self._start()

    def _start(self) -> None:
        if self.remaining <= 0:
            self.remaining = self.phase_total
        self.deadline = time.monotonic() + self.remaining
        self.running = True
        self._render()
        self._schedule()

    def _pause(self) -> None:
        if not self.running:
            return
        self.remaining = max(0.0, self.deadline - time.monotonic())
        self.running = False
        if self._tick_id:
            self.root.after_cancel(self._tick_id)
            self._tick_id = None
        self._render()

    def _schedule(self) -> None:
        if self._tick_id:
            self.root.after_cancel(self._tick_id)
        self._tick_id = self.root.after(150, self._tick)

    def _tick(self) -> None:
        self._tick_id = None
        if not self.running:
            return
        self.remaining = max(0.0, self.deadline - time.monotonic())
        self._render_clock()
        if self.remaining <= 0:
            self._finish_phase()
            return
        self._tick_id = self.root.after(150, self._tick)

    def _finish_phase(self) -> None:
        self.running = False
        self._alert()
        if self.mode == "work":
            self.pomodoros += 1
            nxt = "long" if self.pomodoros % 4 == 0 else "short"
        else:
            nxt = "work"
        self._enter(nxt)
        if self.auto_next.get():
            self._start()

    def _enter(self, mode: str) -> None:
        if self._tick_id:
            self.root.after_cancel(self._tick_id)
            self._tick_id = None
        self.mode = mode
        self.phase_total = float(self.durations[mode] * 60)
        self.remaining = self.phase_total
        self.running = False
        self._render()

    def _reset(self) -> None:
        self._enter(self.mode)

    def _skip(self) -> None:
        self._enter("short" if self.mode == "work" else "work")

    def _alert(self) -> None:
        if winsound is None:
            self.root.bell()
            return
        was_playing = self.noise.enabled
        if was_playing:
            self.noise.pause()
        try:
            winsound.PlaySound(self.chime_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except RuntimeError:
            pass
        if was_playing:
            self.root.after(900, self.noise.resume)

    # -- 渲染 ---------------------------------------------------------------
    @staticmethod
    def _fmt(seconds: float) -> str:
        total = int(math.ceil(max(0.0, seconds)))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _render_clock(self) -> None:
        self.canvas.itemconfig(self.time_text, text=self._fmt(self.remaining))
        frac = 0.0 if self.phase_total <= 0 else max(0.0, self.remaining / self.phase_total)
        self.canvas.itemconfig(self.arc, extent=-359.9 * frac - 0.01)
        self.root.title(f"{self._fmt(self.remaining)} · {MODE_NAMES[self.mode]} — 番茄钟")

    def _render(self) -> None:
        self._render_clock()
        color = ACCENT[self.mode]
        self.canvas.itemconfig(self.arc, outline=color)
        self.canvas.itemconfig(self.mode_text, text=MODE_NAMES[self.mode], fill=color)
        self.canvas.itemconfig(
            self.round_text,
            text=(
                f"第 {self.pomodoros % 4 + 1} 个番茄"
                if self.mode == "work"
                else f"已专注 {self.pomodoros} 个番茄"
            ),
        )
        if self.running:
            label = "暂停"
        elif self.remaining < self.phase_total - 0.01:
            label = "继续"
        else:
            label = "开始"
        self.start_btn.config(text=label, bg=color, activebackground=color)
        self.session_label.config(text=f"已完成 {self.pomodoros} 个")

    # -- 交互 ---------------------------------------------------------------
    def _toggle_noise(self) -> None:
        if self.noise.enabled:
            self.noise.disable()
            self.noise_btn.config(text="开启", bg=CHIP, fg=FG)
        else:
            self.noise.enable(self.noise_kind.get(), self.noise_level.get())
            self.noise_btn.config(text="关闭", bg="#3f4a5e", fg=FG)

    def _on_noise_change(self) -> None:
        if self.noise.enabled:
            self.noise.enable(self.noise_kind.get(), self.noise_level.get())

    def _on_duration_change(self, key: str) -> None:
        if self._sync_guard:
            return
        try:
            value = int(self.dur_vars[key].get())
        except (tk.TclError, ValueError):
            return
        value = max(1, min(180, value))
        self.durations[key] = value
        if key == self.mode and not self.running:
            self.phase_total = float(value * 60)
            self.remaining = self.phase_total
            self._render()

    def _on_close(self) -> None:
        self.running = False
        if self._tick_id:
            self.root.after_cancel(self._tick_id)
        self.noise.disable()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    PomodoroApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
