#!/usr/bin/env python3
"""Draw the 320x170 panel from the same bitmaps the firmware uses."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_font

W, H = 320, 170
PAD, RIGHT = 14, 306
STATUS_Y, DOT = 4, (20, 12)
LABEL1_Y, BAR1_Y, BAR_H, SUB1_Y = 24, 42, 10, 54
LABEL2_Y, BAR2_Y, SUB2_Y = 70, 88, 100
SUMMARY_Y = 116
FOOT_Y = 150

BG = (11, 18, 32)
ORANGE = (245, 165, 36)
GREEN = (61, 220, 106)
AMBER = (230, 178, 60)
RED = (255, 93, 93)
TRACK = (44, 53, 72)
TEXT = (230, 234, 242)
DIM = (139, 149, 168)
CLOCK = (154, 163, 181)
WHITE = (255, 255, 255)
BLUE = (76, 154, 255)


def blend(fg, bg, alpha):
    if alpha >= 15:
        return fg
    if alpha <= 0:
        return bg
    inv = 15 - alpha
    return tuple((fg[i] * alpha + bg[i] * inv) // 15 for i in range(3))


def bar_color(percent):
    if percent >= 85:
        return RED
    if percent >= 55:
        return AMBER
    return GREEN


class Face:
    def __init__(self, glyphs, line_height):
        self.glyphs = {glyph["cp"]: glyph for glyph in glyphs}
        self.line_height = line_height
        self.pixels = {}
        for glyph in glyphs:
            levels = glyph["levels"]
            self.pixels[glyph["cp"]] = levels

    def width(self, text):
        total = 0
        for char in text:
            glyph = self.glyphs.get(ord(char))
            total += glyph["adv"] if glyph else self.line_height // 2
        return total


def raster_with_levels(font_path, size, codepoints):
    glyphs, _blob, line_height = gen_font.rasterize(font_path, size, codepoints)
    # rasterize() does not keep levels. Rebuild quickly from the packed path by
    # calling a local copy: gen_font.rasterize already dropped levels.
    # Re-read by decoding the nibble blob using offsets.
    return glyphs, line_height


def attach_levels(glyphs, blob):
    for glyph in glyphs:
        count = glyph["w"] * glyph["h"]
        raw = blob[glyph["offset"] :]
        levels = []
        for index in range(count):
            byte = raw[index >> 1]
            levels.append((byte >> 4) if index % 2 == 0 else (byte & 0x0F))
        glyph["levels"] = levels
    return glyphs


def draw_text(image, face, x, y, text, color, max_x):
    px = image.load()
    for char in text:
        glyph = face.glyphs.get(ord(char))
        if glyph is None or x >= max_x:
            x += face.line_height // 2
            continue
        levels = glyph["levels"]
        cursor = 0
        for row in range(glyph["h"]):
            for col in range(glyph["w"]):
                alpha = levels[cursor]
                cursor += 1
                sx = x + glyph["x"] + col
                sy = y + glyph["y"] + row
                if alpha and 0 <= sx < W and 0 <= sy < H and sx < max_x:
                    px[sx, sy] = blend(color, px[sx, sy], alpha)
        x += glyph["adv"]
    return x


def rounded(draw, box, radius, color):
    draw.rounded_rectangle(box, radius=radius, fill=color)


def battery_color(state):
    kind = state.get("battery_kind", "level")
    if kind != "level":
        return CLOCK
    percent = state.get("battery", 0)
    if percent <= 15:
        return RED
    if percent <= 40:
        return AMBER
    return GREEN


def draw_battery(draw, image, face_sub, x, y, state):
    color = battery_color(state)
    body = (x, y, x + 15, y + 8)
    draw.rounded_rectangle(body, radius=1, outline=color)
    draw.rectangle((x + 16, y + 2, x + 17, y + 6), fill=color)
    if state.get("battery_kind", "level") == "level" and state.get("battery", 0) > 0:
        fill = max(1, int(12 * min(state["battery"], 100) / 100))
        draw.rectangle((x + 2, y + 2, x + 2 + fill - 1, y + 6), fill=color)
    text = state.get("battery_text", "--")
    draw_text(image, face_sub, x + 20, STATUS_Y + 2, text, color, W)


def render(face_ui, face_sub, face_num, state):
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    accent = {"orange": ORANGE, "green": GREEN, "red": RED}.get(state["color"], CLOCK)
    label = state["label"]
    if state["link"] == "wifi":
        label, accent = "未连接无线网络", RED
    elif state["link"] == "stale":
        label, accent = "连接中断", RED
    clock = state.get("clock", "08:27")
    clock_x = RIGHT - face_ui.width(clock)
    battery_text = state.get("battery_text", "--")
    battery_w = 18 + 2 + face_sub.width(battery_text)
    battery_x = clock_x - 8 - battery_w
    draw.ellipse((DOT[0] - 4, DOT[1] - 4, DOT[0] + 4, DOT[1] + 4), fill=accent)
    draw_text(image, face_ui, 30, STATUS_Y, label, accent, battery_x - 6)
    draw_battery(draw, image, face_sub, battery_x, STATUS_Y + 4, state)
    draw_text(image, face_ui, clock_x, STATUS_Y, clock, CLOCK, W)
    draw_text(image, face_ui, PAD, LABEL1_Y, "本周额度", TEXT, W)
    pct = "--" if state["weekly"] < 0 else f"{int(state['weekly'] + 0.5)}%"
    num_y = LABEL1_Y + (face_ui.line_height - face_num.line_height) // 2
    draw_text(image, face_num, RIGHT - face_num.width(pct), num_y, pct, WHITE, W)
    rounded(draw, (PAD, BAR1_Y, RIGHT, BAR1_Y + BAR_H), BAR_H // 2, TRACK)
    if state["weekly"] > 0:
        fill = int((RIGHT - PAD) * min(state["weekly"], 100) / 100 + 0.5)
        rounded(draw, (PAD, BAR1_Y, PAD + max(fill, 2), BAR1_Y + BAR_H), BAR_H // 2, bar_color(state["weekly"]))
    draw_text(image, face_sub, PAD, SUB1_Y, state["weekly_sub"], DIM, RIGHT)
    draw_text(image, face_ui, PAD, LABEL2_Y, "上下文", TEXT, W)
    pct = "--" if state["context"] < 0 else f"{int(state['context'] + 0.5)}%"
    draw_text(image, face_num, RIGHT - face_num.width(pct), LABEL2_Y + (face_ui.line_height - face_num.line_height) // 2, pct, WHITE, W)
    rounded(draw, (PAD, BAR2_Y, RIGHT, BAR2_Y + BAR_H), BAR_H // 2, TRACK)
    if state["context"] > 0:
        fill = int((RIGHT - PAD) * min(state["context"], 100) / 100 + 0.5)
        rounded(draw, (PAD, BAR2_Y, PAD + max(fill, 2), BAR2_Y + BAR_H), BAR_H // 2, bar_color(state["context"]))
    draw_text(image, face_sub, PAD, SUB2_Y, state["context_sub"], DIM, RIGHT)
    summary = state.get("summary") or ""
    if summary and state["link"] == "ok":
        draw_text(image, face_sub, PAD, SUMMARY_Y, summary, DIM, RIGHT)
    footer_max = RIGHT
    page = state.get("page")
    if page and state["link"] == "ok":
        badge_w = face_sub.width(page)
        draw_text(image, face_sub, RIGHT - badge_w, FOOT_Y + 2, page, CLOCK, W)
        footer_max = RIGHT - badge_w - 6
    draw_text(image, face_ui, PAD, FOOT_Y, state["footer"], BLUE, footer_max)
    return image


def main():
    font_path = gen_font.find_font()
    codepoints = gen_font.collect_codepoints()
    numeric = [cp for cp in codepoints if chr(cp) in " 0123456789%.-"]
    ui_g, ui_b, ui_h = gen_font.rasterize(font_path, 16, codepoints)
    sub_g, sub_b, sub_h = gen_font.rasterize(font_path, 12, codepoints)
    num_g, num_b, num_h = gen_font.rasterize(font_path, 18, numeric)
    faces = (
        Face(attach_levels(ui_g, ui_b), ui_h),
        Face(attach_levels(sub_g, sub_b), sub_h),
        Face(attach_levels(num_g, num_b), num_h),
    )
    states = [
        ("running", {"link": "ok", "label": "执行中 · 跑命令", "color": "orange", "weekly": 16, "weekly_sub": "约 6 天后重置 · Pro", "context": 73.47, "context_sub": "349k / 475k tokens", "footer": "NiceShot · 本轮已运行 4分12秒", "clock": "08:27", "battery_kind": "level", "battery": 76, "battery_text": "76%", "page": "2/4", "summary": "请把桌面状态改一下"}),
        ("thinking", {"link": "ok", "label": "执行中 · 思考中", "color": "orange", "weekly": 16, "weekly_sub": "约 6 天后重置 · Pro", "context": 48, "context_sub": "228k / 475k tokens", "footer": "NiceShot · 本轮已运行 18秒", "clock": "08:27", "battery_kind": "level", "battery": 76, "battery_text": "76%", "summary": "请把桌面状态改一下"}),
        ("complete", {"link": "ok", "label": "已完成", "color": "green", "weekly": 12, "weekly_sub": "约 6 天后重置 · Plus", "context": 2.53, "context_sub": "12k / 475k tokens", "footer": "NiceShot · 本轮已运行 1分30秒", "clock": "08:27", "battery_kind": "level", "battery": 76, "battery_text": "76%", "summary": "请查一下本周额度"}),
        ("interrupted", {"link": "ok", "label": "已中断", "color": "red", "weekly": 20, "weekly_sub": "约 6 天后重置 · Pro", "context": 4.23, "context_sub": "20.1k / 475k tokens", "footer": "NiceShot · 本轮已运行 1分10秒", "clock": "08:27", "battery_kind": "level", "battery": 12, "battery_text": "12%", "summary": "请查一下本周额度"}),
        ("quota", {"link": "ok", "label": "额度将尽 · 思考中", "color": "red", "weekly": 96, "weekly_sub": "约 6 天后重置 · Pro", "context": 95.16, "context_sub": "452k / 475k tokens", "footer": "NiceShot · 本轮已运行 1分6秒", "clock": "08:27", "battery_kind": "level", "battery": 76, "battery_text": "76%", "summary": "请把桌面状态改一下"}),
        ("stale", {"link": "stale", "label": "连接中断", "color": "red", "weekly": 16, "weekly_sub": "约 6 天后重置 · Pro", "context": 73.47, "context_sub": "349k / 475k tokens", "footer": "NiceShot · 上次更新 2 分钟前", "clock": "08:29", "battery_kind": "none", "battery": 0, "battery_text": "无电"}),
        ("offline", {"link": "wifi", "label": "未连接无线网络", "color": "red", "weekly": -1, "weekly_sub": "暂无数据", "context": -1, "context_sub": "暂无数据", "footer": "请检查无线网络", "clock": "--:--", "battery_kind": "usb", "battery": 0, "battery_text": "USB"}),
    ]
    out = Path(__file__).resolve().parents[1] / "preview"
    out.mkdir(exist_ok=True)
    tiles = []
    for name, state in states:
        image = render(*faces, state)
        image.save(out / f"{name}.png")
        tiles.append(image)
    sheet = Image.new("RGB", (W, H * len(tiles) + 8 * (len(tiles) - 1)), (30, 32, 40))
    y = 0
    for tile in tiles:
        sheet.paste(tile, (0, y))
        y += H + 8
    sheet_dir = Path("/tmp/codex-desk-preview")
    sheet_dir.mkdir(exist_ok=True)
    sheet_path = sheet_dir / "sheet.png"
    sheet.save(sheet_path)
    print(out)
    print(sheet_path)


if __name__ == "__main__":
    main()
