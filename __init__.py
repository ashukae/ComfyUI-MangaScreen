"""Manga Finish: turns an illustration (or an AI "make it manga" edit) into a clean black-and-white
manga page. Keeps the drawing (lines, hatching, where it is dark), throws away every dot or soft grey
the image already has, pushes the values into a few flat regions and prints them with a real screen.

Manga Levels: grayscale + levels, the light path when no edit model is used.

Manga Panel: frames the finished page as a panel (crop, border, margin) with a light paper/ink look.
"""
import cv2
import numpy as np
import torch

ANGLE = 45.0


def print_screen(cover, pitch):
    """Page-aligned AM dots for a coverage map, rendered 3x supersampled so dots are round."""
    h, w = cover.shape
    s = 3
    y, x = np.mgrid[0:h * s, 0:w * s].astype(np.float32)
    a = np.deg2rad(ANGLE)
    u = (x * np.cos(a) + y * np.sin(a)) / (pitch * s)
    v = (-x * np.sin(a) + y * np.cos(a)) / (pitch * s)
    spot = 1 - (np.cos(2 * np.pi * u) + np.cos(2 * np.pi * v) + 2) / 4
    c = cv2.resize(cover, (w * s, h * s), interpolation=cv2.INTER_LINEAR)
    return cv2.resize((spot < c).astype(np.float32), (w, h), interpolation=cv2.INTER_AREA) > 0.5


def ink_layer(L, k):
    """Lines and solid blacks as a crisp mask. k is the image scale relative to 1024 px width."""
    solid = cv2.GaussianBlur(L, (0, 0), 1.5 * k) < 0.22
    Lb = cv2.GaussianBlur(L, (0, 0), 0.7 * k)
    line = (Lb < cv2.blur(Lb, (int(15 * k) | 1,) * 2) - 0.07) & (Lb < 0.65)
    _, lab, st, _ = cv2.connectedComponentsWithStats(line.astype(np.uint8), connectivity=8)
    w, h, area = st[:, cv2.CC_STAT_WIDTH], st[:, cv2.CC_STAT_HEIGHT], st[:, cv2.CC_STAT_AREA]
    # a stroke is long and thin; a dot, or a clump of generated dots, is compact
    keep = (np.maximum(w, h) >= 10 * k) & (area >= 12 * k * k) & (area < 0.6 * w * h + 40 * k * k)
    keep |= area >= 400 * k * k
    keep[0] = False
    ink = solid | keep[lab]
    _, lab, st, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    big = st[:, cv2.CC_STAT_AREA] >= 6 * k * k
    big[0] = False
    return big[lab]


def value_map(L, ink, k):
    """Descreened value with the ink masked out (normalized convolution), so lines leave no dark
    halo for the screen to pick up, then edge-preserving flattening."""
    m = (~ink).astype(np.float32)
    s = 2.5 * k
    V = cv2.GaussianBlur(L * m, (0, 0), s) / np.maximum(cv2.GaussianBlur(m, (0, 0), s), 1e-3)
    for _ in range(2):
        V = cv2.bilateralFilter(V, int(9 * k) | 1, 0.10, 9 * k)
    return V


def tone_plan(V, k, white, split, black, min_region=600):
    """0 white, 1 light tone, 2 dark tone, 3 black. Regions under min_region px (at 1024 px width)
    melt into their surroundings, so every tone is a deliberate flat area."""
    level = cv2.medianBlur(np.digitize(-V, [-white, -split, -black]).astype(np.uint8), int(11 * k) | 1)
    for lv in (1, 2, 3, 0):
        _, lab, st, _ = cv2.connectedComponentsWithStats((level == lv).astype(np.uint8), connectivity=4)
        small = st[:, cv2.CC_STAT_AREA] < min_region * k * k
        small[0] = False
        hole = small[lab]
        if hole.any():
            fill = cv2.medianBlur(np.where(hole, 255, level).astype(np.uint8), int(21 * k) | 1)
            level = np.where(hole & (fill != 255), fill, level).astype(np.uint8)
    return level


def region_spread(level, V):
    """Standard deviation of V inside each flat region of the plan: a region that hides a gradient spreads wide."""
    out = np.zeros_like(V)
    for lv in range(4):
        n, lab = cv2.connectedComponents((level == lv).astype(np.uint8), connectivity=4)
        cnt = np.maximum(np.bincount(lab.ravel(), minlength=n), 1)
        mean = np.bincount(lab.ravel(), V.ravel(), minlength=n) / cnt
        std = np.sqrt(np.maximum(np.bincount(lab.ravel(), (V * V).ravel(), minlength=n) / cnt - mean * mean, 0))
        out = np.where(level == lv, std[lab], out)
    return out.astype(np.float32)


def white_cut(candidates, k):
    """The strokes worth keeping in white inside beta: long ones only, thinned, so a dark coat shows
    its main folds and seams instead of looking like an inverted line drawing."""
    _, lab, st, _ = cv2.connectedComponentsWithStats(candidates.astype(np.uint8), connectivity=8)
    keep = np.maximum(st[:, cv2.CC_STAT_WIDTH], st[:, cv2.CC_STAT_HEIGHT]) >= 40 * k
    keep[0] = False
    return cv2.erode(keep[lab].astype(np.uint8), np.ones((2 if k < 1.6 else 3,) * 2, np.uint8)) > 0


def finish(img, lines, white, black, light_tone, dark_tone, gradation, pen, pen_threshold, white_lines):
    """pen: line strength map (0..1) drawn on top, or None."""
    L = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    k = L.shape[1] / 1024
    ink = ink_layer(L, k)
    strokes = ink & ~(cv2.GaussianBlur(L, (0, 0), 1.5 * k) < 0.22)      # lines without the solid masses
    if pen is not None:
        strokes |= pen > pen_threshold
    V = value_map(L, ink, k)
    level = tone_plan(V, k, white, (white + black) / 2, black)
    cover = np.take(np.float32((0.0, light_tone, dark_tone, 1.0)), level)
    if gradation:
        # gradation tone: where a flat region hides a ramp, the dot area follows the darkness instead
        Vs = cv2.GaussianBlur(V, (0, 0), 3 * k)
        a = cv2.GaussianBlur(np.clip((region_spread(level, Vs) - 0.03) / 0.05, 0, 1) * gradation, (0, 0), 4 * k)
        cover = cover * (1 - a) + np.clip((white - Vs) / (white - black), 0, 1) * a
    page = print_screen(cover, L.shape[1] / lines) | ink | strokes
    if white_lines:
        # black on black is invisible: inside beta the main strokes (folds, seams, zips) are cut out
        # in white, the way a dark coat is inked; the region is shrunk so its outline stays black
        beta = cv2.erode((level == 3).astype(np.uint8), np.ones((int(3 * k) | 1,) * 2, np.uint8)) > 0
        candidates = strokes if pen is None else pen > 1 - white_lines
        page = (page | beta) & ~(beta & white_cut(candidates, k))
    return ~page


def per_frame(image, fn):
    out = []
    for i, frame in enumerate(image):
        img = (frame[..., :3].cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
        out.append(torch.from_numpy(fn(img, i).astype(np.float32)))
    return (torch.stack(out)[..., None].expand(-1, -1, -1, 3).contiguous().to(image.device),)


def pen_map(lines, i, shape):
    """White-on-black line map (Manga Line Extraction) resized to the frame, as line strength 0..1."""
    m = lines[min(i, lines.shape[0] - 1), ..., :3].mean(-1).cpu().numpy().astype(np.float32)
    return cv2.resize(m, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)


class MangaFinish:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "screen_lines": ("INT", {"default": 200, "min": 40, "max": 600, "step": 10,
                                     "tooltip": "Rows of dots across the image width. 120 coarse, 200 normal, 300 fine. The same value looks the same at any resolution."}),
            "white_point": ("FLOAT", {"default": 0.80, "min": 0.3, "max": 1.0, "step": 0.01,
                                      "tooltip": "Brighter than this becomes paper white. Lower it to clear more tone (skin, light hair)."}),
            "black_point": ("FLOAT", {"default": 0.30, "min": 0.0, "max": 0.7, "step": 0.01,
                                      "tooltip": "Darker than this becomes solid black. Raise it for more beta."}),
            "light_tone": ("FLOAT", {"default": 0.18, "min": 0.02, "max": 0.9, "step": 0.01}),
            "dark_tone": ("FLOAT", {"default": 0.42, "min": 0.02, "max": 0.95, "step": 0.01}),
            "gradation": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05,
                                    "tooltip": "Gradation tone: where the original fades smoothly (a shadow deepening into black), dots grow with the darkness instead of being cut into flat steps. 0 keeps only flat areas."}),
            "pen_threshold": ("FLOAT", {"default": 0.30, "min": 0.05, "max": 0.95, "step": 0.05,
                                        "tooltip": "Only with pen_lines connected. Lower draws more of its lines on top (0.25 folds, outlines inside tone and hatched blush; 0.5 only the clearest)."}),
            "white_lines": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 1.0, "step": 0.05,
                                      "tooltip": "Inside solid black areas, the main lines are cut out thin and white so a dark coat keeps its folds, seams and zips. Higher keeps more of them (with pen_lines: 0.2 only the strongest, 0.4 main folds, 1.0 all). 0 turns it off. Raise black_point to send dark clothes to beta."}),
        }, "optional": {
            "pen_lines": ("IMAGE", {"tooltip": "Line map drawn on top of the finished page, e.g. Manga Lineart (Manga Line Extraction) run on the same image."}),
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "apply"
    CATEGORY = "image/postprocessing"

    def apply(self, image, screen_lines, white_point, black_point, light_tone, dark_tone, gradation, pen_threshold, white_lines, pen_lines=None):
        def run(img, i):
            pen = None if pen_lines is None else pen_map(pen_lines, i, img.shape[:2])
            return finish(img, screen_lines, white_point, black_point, light_tone, dark_tone, gradation, pen, pen_threshold, white_lines)
        return per_frame(image, run)


FRAMES = {"original": None, "4:3 wide": 4 / 3, "16:9 wide": 16 / 9, "3:4 tall": 3 / 4, "1:1": 1.0}
PAPERS = {"white": ((255, 255, 255), 0.0), "off-white": ((251, 250, 247), 1.5), "cream": ((246, 243, 236), 3.0)}
INK_RGB = np.float32((26, 25, 28))


def noise(h, w, scales, seed):
    rng = np.random.default_rng(seed)
    out = np.zeros((h, w), np.float32)
    for s, amp in scales:
        n = rng.standard_normal((max(2, h // s), max(2, w // s))).astype(np.float32)
        out += amp * cv2.resize(n, (w, h), interpolation=cv2.INTER_CUBIC)
    return out


def cut_panel(ink, ratio, x, y, border, margin):
    """Crop the largest window of the given aspect at (x, y), draw the panel border, add the page margin."""
    h, w = ink.shape
    cw, ch = (w, h) if ratio is None else ((w, round(w / ratio)) if w / h <= ratio else (round(h * ratio), h))
    cw, ch = min(cw, w), min(ch, h)
    x0, y0 = round((w - cw) * x), round((h - ch) * y)
    p = ink[y0:y0 + ch, x0:x0 + cw].copy()
    p[:border] = p[-border:] = True
    p[:, :border] = p[:, -border:] = True
    return np.pad(p, margin, constant_values=False)


def print_look(ink, paper, strength, k):
    """Ink spreads a fraction of a pixel and its density mottles a little; paper colour and fibre."""
    h, w = ink.shape
    a = ink.astype(np.float32)
    if strength:
        a = np.clip((cv2.GaussianBlur(a, (0, 0), 0.6 * k * strength) - 0.35) / 0.4, 0, 1)
        a *= 1 - 0.05 * strength * (0.5 + 0.5 * np.tanh(noise(h, w, [(int(64 * k), 1.0), (int(16 * k), 0.5)], 1)))
    rgb, fibre = PAPERS[paper]
    base = np.float32(rgb)[None, None] + fibre * noise(h, w, [(3, 1.0), (1, 0.6)], 2)[..., None]
    return np.clip(base * (1 - a[..., None]) + INK_RGB * strength * a[..., None], 0, 255)


class MangaPanel:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE", {"tooltip": "A finished black-and-white page (Manga Finish output)."}),
            "frame": (list(FRAMES), {"default": "4:3 wide", "tooltip": "Panel shape cut out of the image."}),
            "crop_x": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.05, "tooltip": "Where the panel sits horizontally: 0 left edge, 1 right edge."}),
            "crop_y": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 1.0, "step": 0.05, "tooltip": "Where the panel sits vertically: 0 top, 1 bottom."}),
            "border": ("INT", {"default": 5, "min": 0, "max": 40, "tooltip": "Panel border thickness in pixels at 1024 px width."}),
            "margin": ("INT", {"default": 60, "min": 0, "max": 400, "tooltip": "Paper around the panel in pixels at 1024 px width."}),
            "paper": (list(PAPERS), {"default": "white"}),
            "ink": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.05,
                              "tooltip": "Printed-ink feel: soft ink edges and slightly uneven solid blacks. 0 keeps pure black and white."}),
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "apply"
    CATEGORY = "image/postprocessing"

    def apply(self, image, frame, crop_x, crop_y, border, margin, paper, ink):
        out = []
        for f in image:
            page = f[..., :3].mean(-1).cpu().numpy() < 0.5
            k = page.shape[1] / 1024
            framed = cut_panel(page, FRAMES[frame], crop_x, crop_y, round(border * k), round(margin * k))
            out.append(torch.from_numpy(print_look(framed, paper, ink, k) / 255))
        return (torch.stack(out).float().to(image.device),)


class MangaLevels:
    """Grayscale + levels: the light path without an edit model. Brightens until skin and light
    colours turn to paper, so Manga Finish only tones what is really dark."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "black": ("FLOAT", {"default": 0.20, "min": 0.0, "max": 0.9, "step": 0.01, "tooltip": "Input darker than this becomes black."}),
            "white": ("FLOAT", {"default": 0.70, "min": 0.1, "max": 1.0, "step": 0.01,
                                "tooltip": "Input brighter than this becomes white. Lower it until skin and light hair lose their tone."}),
            "gamma": ("FLOAT", {"default": 1.3, "min": 0.3, "max": 3.0, "step": 0.05, "tooltip": "Above 1 brightens the mid tones."}),
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "apply"
    CATEGORY = "image/postprocessing"

    def apply(self, image, black, white, gamma):
        g = (image[..., :3] * torch.tensor([0.299, 0.587, 0.114], device=image.device, dtype=image.dtype)).sum(-1, keepdim=True)
        g = ((g - black) / max(white - black, 1e-3)).clamp(0, 1) ** (1 / gamma)
        return (g.expand(-1, -1, -1, 3).contiguous(),)


NODE_CLASS_MAPPINGS = {"MangaLevels": MangaLevels, "MangaFinish": MangaFinish, "MangaPanel": MangaPanel}
NODE_DISPLAY_NAME_MAPPINGS = {"MangaLevels": "Manga Levels", "MangaFinish": "Manga Finish", "MangaPanel": "Manga Panel"}
