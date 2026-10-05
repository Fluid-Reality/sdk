"""Render five hand-demo UI concept previews as SVG and PNG files.

The hand silhouette adapts Cy21's "Hand left.svg" (CC BY-SA 3.0):
https://commons.wikimedia.org/wiki/File:Hand_left.svg
It is filled and mirrored so the pinky appears on the left and thumb on the right.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
import subprocess

OUT = Path(__file__).resolve().parent
W, H = 1280, 800
HAND_PATH = (
    "M67.265 203.297c4.001 17.025 13.368 32.77 24.919 43.293 12.779 11.641 27.907 5.285 41.089 9.52 "
    "9.405 3.021 17.901 6.807 28.714 3.371 16.238-5.16 38.154-46.172 42.324-61.123 "
    "2.029-7.266 45.414-39.908 56.35-47.131 16.418-10.84 8.15-28.834-9.289-19.791 "
    "-11.855 6.148-51.602 40.602-57.951 34.059s36.33-73.709 43.051-83.852 "
    "c10.572-15.958-10.541-26.846-19.885-13.199-5.885 8.592-44.021 77.211-50.424 74.564 "
    "-6.403-2.646 8.253-81.105 10.439-94.6 3.707-22.875-20.402-27.361-24.219-4.039 "
    "-2.169 13.25-13.406 91.547-19.883 92.725s-26.693-67.764-31.055-82.861 "
    "c-5.918-20.477-27.516-13.882-22.154 6.934 3.877 15.059 23.036 81.072 16.934 103.57 "
    "-3.52 12.971-6.746 11.518-16.583-.264-26.474-31.703-55.667-34.346-63.162-17.869 "
    "17.558 10.337 45.353 33.576 50.785 56.693z"
)
TIP_COORDS = ((57, 258), (110, 143), (214, 67), (352, 90), (462, 255))
FINGER_NAMES = ("Pinky", "Ring", "Middle", "Index", "Thumb")
FONT = "'Segoe UI',Arial,sans-serif"
MONO = "'Cascadia Code',Consolas,monospace"


def r(x, y, w, h, fill, radius=0, stroke="none", sw=1, extra=""):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {extra}/>' )


def t(x, y, value, size=16, color="#111827", weight=400, anchor="start", family=FONT,
      extra=""):
    return (f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}" font-family="{family}" '
            f'{extra}>{escape(value)}</text>')


def c(x, y, radius, fill, stroke="none", sw=1, extra=""):
    return (f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{sw}" {extra}/>' )


def ln(x1, y1, x2, y2, color, sw=1, extra=""):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{color}" stroke-width="{sw}" {extra}/>' )


def hand(x, y, size, fill, stroke="none", stroke_width=0, tip_style="disc",
         active=2, idle="#f4e6da", positive="#ee584f", negative="#4c8bf5"):
    scale = size / 512
    parts = [f'<g transform="translate({x} {y}) scale({scale})">',
             f'<g transform="translate(512 0) scale(-1.79 1.79)"><path d="{HAND_PATH}" '
             f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}" '
             f'stroke-linejoin="round"/></g>']
    for index, (px, py) in enumerate(TIP_COORDS):
        is_active = index == active
        tone = positive if index != 4 else negative
        marker = tone if is_active else idle
        if tip_style == "disc":
            if is_active:
                parts.append(c(px, py, 40, tone, extra='opacity="0.18"'))
            parts.append(c(px, py, 25, marker, "#ffffff" if is_active else "#c4a797", 3))
            parts.append(t(px, py+7, str(index), 22, "white" if is_active else "#6c4c40", 700,
                           "middle"))
        elif tip_style == "ring":
            if is_active:
                parts.append(c(px, py, 43, tone, extra='opacity="0.15"'))
            parts.append(c(px, py, 29, "#12252d", tone if is_active else "#56717f", 4))
            parts.append(c(px, py, 23, tone if is_active else "#142b34", extra='opacity="0.82"'))
            parts.append(t(px, py+7, str(index), 22, "white" if is_active else "#adbfca", 700,
                           "middle"))
        elif tip_style == "pin":
            parts.append(c(px, py, 30, "#fffdf8", tone if is_active else "#dad5cc", 2))
            parts.append(c(px, py, 24, marker))
            parts.append(t(px, py+6, str(index), 20, "white" if is_active else "#6f6254", 700,
                           "middle"))
        elif tip_style == "target":
            parts.append(c(px, py, 33, "none", tone if is_active else "#50a6bd", 2))
            parts.append(c(px, py, 21, tone if is_active else "#173b53", tone if is_active else "#50a6bd", 2))
            parts.append(t(px, py+6, str(index), 19, "white" if is_active else "#a4d7e3", 700,
                           "middle", MONO))
        elif tip_style == "soft":
            parts.append(c(px, py, 32, tone if is_active else "#e7e9ec", extra='opacity="0.55"'))
            parts.append(c(px, py, 23, tone if is_active else "#f8f9fa"))
            parts.append(t(px, py+6, str(index), 19, "white" if is_active else "#7d8794", 700,
                           "middle"))
    parts.append("</g>")
    return "".join(parts)


def arrow(x, y, color):
    return f'<path d="M{x} {y}l5 5 5-5" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'


def waveform(x, y, w, h, color, kind="wave"):
    points = []
    if kind == "wave":
        for step in range(61):
            px = x + w * step / 60
            py = y + h/2 - (h*.32) * __import__("math").sin(step / 60 * 4 * __import__("math").pi)
            points.append(f"{px:.1f},{py:.1f}")
    elif kind == "snap":
        for cycle in range(3):
            base = x + w * cycle / 3
            points += [f"{base:.1f},{y+h*.15:.1f}", f"{base+w/3-3:.1f},{y+h*.85:.1f}"]
    else:
        for cycle in range(4):
            base = x + w * cycle / 4
            points += [f"{base:.1f},{y+h*.2:.1f}", f"{base+w/8:.1f},{y+h*.2:.1f}",
                       f"{base+w/8:.1f},{y+h*.8:.1f}", f"{base+w/4:.1f},{y+h*.8:.1f}"]
    return f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'


DEFS = """<defs>
<linearGradient id="skin" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#f5d9c7"/><stop offset="1" stop-color="#e9bda4"/></linearGradient>
<linearGradient id="skin2" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#eed4b8"/><stop offset="1" stop-color="#d6aa8c"/></linearGradient>
<linearGradient id="darkhand" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#17333e"/><stop offset="1" stop-color="#0e1b28"/></linearGradient>
<linearGradient id="glove" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#ecebea"/><stop offset="1" stop-color="#bdc8cf"/></linearGradient>
</defs>"""


def svg(contents, bg):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">{DEFS}{r(0,0,W,H,bg)}{contents}</svg>'


def concept_1():
    p = [r(0, 0, 1280, 78, "#ffffff"), ln(0, 78, 1280, 78, "#e9edf2"),
         c(44, 39, 18, "#ee594e"), t(44, 45, "F", 18, "white", 800, "middle"),
         t(77, 46, "FLUID REALITY", 17, "#1c2836", 800),
         t(246, 46, "/   HAND DEMO", 15, "#86929f", 600),
         c(1090, 39, 5, "#2bbd79"), t(1107, 45, "COM22 connected", 14, "#445568", 600),
         r(34, 106, 754, 656, "#ffffff", 22, "#e5e9ee"),
         t(70, 151, "Live hand", 24, "#1a2a3a", 700),
         t(70, 177, "Fingertips reflect the output of each mapped actuator", 14, "#8391a0"),
         r(69, 204, 684, 490, "#f8fafb", 20),
         hand(207, 214, 470, "url(#skin)", "#a8786b", 2.5, "disc", 2),
         c(78, 729, 5, "#ee584f"), t(92, 734, "Positive", 13, "#667789"),
         c(182, 729, 5, "#4c8bf5"), t(196, 734, "Negative", 13, "#667789"),
         r(814, 106, 432, 656, "#ffffff", 22, "#e5e9ee"),
         t(846, 154, "Demo controls", 22, "#1a2a3a", 700),
         t(846, 196, "PATTERN", 12, "#8190a0", 700),
         r(846, 210, 368, 56, "#f6f8fa", 12, "#dfe5eb"),
         t(864, 245, "Slow Wave", 18, "#1a2a3a", 600), arrow(1180, 234, "#556474"),
         r(846, 286, 368, 105, "#f5f8fa", 12),
         t(865, 316, "SEQUENCE PREVIEW", 11, "#8493a2", 700),
         waveform(866, 330, 325, 42, "#ee584f", "wave"),
         t(846, 431, "FINGER MAPPING", 12, "#8190a0", 700)]
    for i, name in enumerate(FINGER_NAMES):
        yy = 465 + i * 45
        p += [t(846, yy, name, 16, "#27394a", 500),
              r(1138, yy-24, 76, 34, "#f7f9fb", 8, "#e2e7ec"),
              t(1162, yy, str(i), 15, "#263849", 600), arrow(1192, yy-7, "#7f8d9b")]
    p += [r(846, 698, 368, 45, "#ee594e", 11), t(1030, 727, "Run demo", 17, "white", 700, "middle")]
    return svg("".join(p), "#f3f5f7")


def concept_2():
    p = [r(0, 0, W, 72, "#0c141e"), ln(0, 72, W, 72, "#243542"),
         t(36, 46, "FLUID  /  SIGNAL", 20, "#eff7fb", 700, family=MONO),
         r(1080, 19, 162, 33, "#193b32", 16), c(1100, 36, 5, "#53e4a4"),
         t(1115, 41, "BOARD ONLINE", 12, "#a5e3c9", 700, family=MONO),
         r(28, 98, 820, 676, "#111f2c", 19, "#2d4351"),
         t(62, 141, "LIVE OUTPUT", 14, "#8aaeb8", 700, family=MONO),
         t(62, 177, "Five-channel hand", 26, "#f1f8fa", 600),
         r(61, 202, 754, 492, "#0d1a26", 15, "#28404e")]
    for xx in range(89, 815, 32):
        p.append(ln(xx, 203, xx, 693, "#213241", 0.7))
    for yy in range(227, 692, 32):
        p.append(ln(62, yy, 814, yy, "#213241", 0.7))
    p += [hand(204, 214, 477, "url(#darkhand)", "#638c9e", 2.2,
               "ring", 4, "#17313e", "#ff6968", "#50b2ff"),
          r(61, 712, 754, 43, "#182b37", 8),
          t(78, 739, "OUTPUT", 11, "#8aaeb8", 700, family=MONO),
          waveform(170, 718, 625, 29, "#ff6968", "snap"),
          r(873, 98, 379, 676, "#111f2c", 19, "#2d4351"),
          t(905, 142, "PATTERN ENGINE", 13, "#8aaeb8", 700, family=MONO),
          t(905, 189, "Snap", 30, "#f2f9fa", 700),
          t(905, 218, "255 → -255 in 1.0 s", 14, "#8aaeb8", 500, family=MONO),
          r(905, 245, 315, 93, "#0e1b27", 10, "#294252"),
          waveform(924, 266, 277, 53, "#ff6968", "snap"),
          t(905, 381, "CHANNEL ASSIGNMENT", 13, "#8aaeb8", 700, family=MONO)]
    for i, name in enumerate(FINGER_NAMES):
        yy = 417 + i*49
        p += [c(919, yy-5, 11, "#24414e"), t(919, yy, str(i), 11, "#c4e6eb", 700, "middle", MONO),
              t(943, yy, name.upper(), 14, "#e4f0f4", 600, family=MONO),
              r(1139, yy-23, 79, 34, "#1a3342", 6, "#345261"),
              t(1163, yy, str(i), 14, "#e4f0f4", 600, family=MONO), arrow(1194, yy-9, "#a2c4ce")]
    p += [r(905, 695, 315, 48, "#ff6968", 9), t(1062, 726, "RUN DEMO  ▶", 17, "#151b22", 800, "middle", MONO)]
    return svg("".join(p), "#09121b")


def concept_3():
    p = [t(62, 73, "Handscape", 34, "#2f2d2c", 700),
         t(64, 102, "A tactile sequence, made visible.", 16, "#8b8178"),
         r(1054, 38, 162, 41, "#e9f2e9", 20), c(1079, 58, 6, "#4a9b72"),
         t(1095, 64, "Connected", 14, "#386f55", 600),
         r(43, 138, 1194, 600, "#fffdf9", 29, "#ebe5dd"),
         t(80, 182, "CHOOSE A FEEL", 12, "#aa8f7f", 700),
         r(79, 203, 244, 58, "#f7e9df", 13, "#ecd1bd"),
         c(105, 232, 14, "#e79772"), t(136, 238, "Pulse", 18, "#493a35", 600),
         r(79, 272, 244, 58, "#fbf7f1", 13, "#eae4dc"),
         c(105, 301, 14, "#eedccc"), t(136, 307, "Snap", 18, "#493a35", 500),
         r(79, 341, 244, 58, "#fbf7f1", 13, "#eae4dc"),
         c(105, 370, 14, "#eedccc"), t(136, 376, "Slow Wave", 18, "#493a35", 500),
         r(79, 410, 244, 58, "#fbf7f1", 13, "#eae4dc"),
         c(105, 439, 14, "#eedccc"), t(136, 445, "Fast Wave", 18, "#493a35", 500),
         r(79, 479, 244, 58, "#fbf7f1", 13, "#eae4dc"),
         c(105, 508, 14, "#eedccc"), t(136, 514, "Square Wave", 18, "#493a35", 500),
         hand(377, 178, 497, "url(#skin2)", "#b89176", 2, "pin", 2,
              "#fff6ed", "#e98367", "#6e9dc2"),
         t(983, 224, "PULSE", 13, "#b58068", 700),
         t(983, 268, "A gentle rise.", 29, "#3e302c", 700),
         t(983, 302, "A clear release.", 29, "#3e302c", 700),
         t(983, 347, "All five fingertips move", 15, "#8d8076"),
         t(983, 370, "together in a repeating cycle.", 15, "#8d8076"),
         waveform(983, 425, 180, 55, "#e98367", "wave"),
         r(983, 570, 202, 58, "#c65c45", 14),
         t(1084, 608, "Run demo  →", 18, "white", 700, "middle"),
         t(1084, 656, "COM22 · Rockford", 13, "#8d8076", 500, "middle")]
    p += [t(80, 626, "FINGER MAPPING", 12, "#aa8f7f", 700)]
    for i, name in enumerate(FINGER_NAMES):
        xx = 80 + i * 156
        p += [t(xx, 659, name, 14, "#695e56", 600),
              r(xx, 671, 104, 36, "#fff9f2", 9, "#e8dcd0"),
              t(xx+14, 695, str(i), 15, "#493a35", 600),
              arrow(xx+80, 684, "#947d6d")]
    return svg("".join(p), "#f3eee8")


def concept_4():
    p = [t(47, 61, "FLUID / LAB", 20, "#b8f3fb", 700, family=MONO),
         t(1110, 58, "PORT  COM22", 14, "#8ad5e1", 600, family=MONO),
         ln(47, 81, 1233, 81, "#37677a"),
         t(47, 134, "ACTUATOR TO FINGER MAP", 29, "#e5fcff", 700, family=MONO),
         t(47, 163, "Interactive hand schematic  /  five mapped outputs", 14, "#7eb6c5", 400, family=MONO),
         r(47, 190, 772, 552, "#0c2940", 15, "#397184")]
    for xx in range(47, 820, 26):
        p.append(ln(xx, 190, xx, 742, "#20445b", 0.7))
    for yy in range(190, 743, 26):
        p.append(ln(47, yy, 819, yy, "#20445b", 0.7))
    p += [c(435, 473, 250, "none", "#2b6576", 1),
          c(435, 473, 182, "none", "#2b6576", 1),
          hand(206, 217, 479, "#10354d", "#72bbcf", 2.2, "target", 0,
               "#14374e", "#ff6f68", "#55baff"),
          t(70, 710, "VIEW 01 / FRONT", 12, "#80bfcc", 600, family=MONO),
          r(843, 190, 390, 552, "#102b42", 15, "#397184"),
          t(870, 229, "ASSIGNMENTS", 15, "#b8f3fb", 700, family=MONO),
          t(870, 263, "Select an actuator for each digit", 13, "#7eb6c5", 400, family=MONO)]
    for i, name in enumerate(FINGER_NAMES):
        yy = 302 + i*66
        p += [ln(870, yy+28, 1206, yy+28, "#2e5367"),
              t(872, yy, f"0{i}", 13, "#54b8ca", 700, family=MONO),
              t(913, yy, name.upper(), 16, "#e2f5f7", 600, family=MONO),
              r(1116, yy-24, 88, 39, "#19394e", 5, "#477083"),
              t(1141, yy+2, str(i), 16, "#d9f2f5", 700, family=MONO),
              arrow(1180, yy-11, "#86c9d6")]
    p += [t(870, 671, "PATTERN   SLOW WAVE  ⌄", 15, "#b8f3fb", 600, family=MONO),
          r(870, 690, 336, 43, "#62d8e9", 7),
          t(1038, 718, "RUN DEMO", 15, "#0d2a3c", 800, "middle", MONO)]
    return svg("".join(p), "#071d30")


def concept_5():
    p = [r(0, 0, 294, 800, "#202631"),
         t(38, 56, "FR", 30, "#f9f9f7", 800),
         t(83, 53, "HAND DEMO", 14, "#a9b1bd", 700),
         ln(38, 79, 256, 79, "#424b57"),
         t(38, 134, "SESSION", 12, "#929ca9", 700),
         c(45, 163, 5, "#58c995"), t(59, 168, "Rockford  ·  COM22", 15, "#e0e5eb"),
         t(38, 230, "PATTERN", 12, "#929ca9", 700),
         r(38, 247, 218, 53, "#353d4a", 10),
         t(56, 280, "Fast Wave", 18, "#f4f7f9", 600), arrow(226, 269, "#b4bdca"),
         t(38, 359, "FINGER  /  ACTUATOR", 12, "#929ca9", 700)]
    for i, name in enumerate(FINGER_NAMES):
        yy = 394+i*50
        p += [t(38, yy, name, 15, "#d4dbe3", 500),
              r(203, yy-26, 53, 36, "#343d49", 7),
              t(220, yy, str(i), 15, "#f3f6f8", 600), arrow(239, yy-9, "#aab7c3")]
    p += [r(38, 698, 218, 54, "#e4514b", 11),
          t(147, 733, "Run demo", 18, "white", 700, "middle"),
          t(342, 66, "The hand, in motion.", 32, "#26323f", 700),
          t(343, 97, "Tap a finger to inspect its mapped output", 15, "#81909e"),
          r(328, 126, 913, 608, "#f3f6f7", 20),
          hand(523, 172, 500, "url(#glove)", "#a5b3bc", 2, "soft", 3,
               "#f4f6f7", "#e4514b", "#4c8bf5"),
          r(360, 647, 849, 60, "#ffffff", 10, "#e6ebee"),
          c(389, 678, 7, "#e4514b"),
          t(411, 683, "Index finger", 16, "#2c3947", 700),
          t(560, 683, "Actuator 3", 15, "#758391"),
          r(889, 664, 276, 27, "#f1f3f5", 13),
          r(889, 664, 208, 27, "#e4514b", 13),
          t(1179, 683, "75%", 14, "#556473", 700, "end")]
    return svg("".join(p), "#ffffff")


CONCEPTS = (
    ("01_studio", concept_1),
    ("02_signal", concept_2),
    ("03_handscape", concept_3),
    ("04_blueprint", concept_4),
    ("05_focus", concept_5),
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    for name, build in CONCEPTS:
        source = build().encode("utf-8")
        svg_path = OUT / f"{name}.svg"
        svg_path.write_bytes(source)
        if chrome.exists():
            subprocess.run(
                [
                    str(chrome), "--headless=new", "--disable-gpu", "--no-first-run",
                    "--no-default-browser-check", "--hide-scrollbars",
                    "--force-device-scale-factor=1", f"--window-size={W},{H}",
                    f"--screenshot={OUT / f'{name}.png'}", svg_path.as_uri(),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    main()
