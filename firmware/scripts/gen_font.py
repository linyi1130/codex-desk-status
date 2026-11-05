#!/usr/bin/env python3
"""Rasterize the desk UI glyphs from Noto Sans SC into firmware/src/font_zh.cpp.

The ESP32 build does not need the .otf. Re-run this if on-screen copy changes:

    FONT_PATH=/tmp/fonts/NotoSansSC-Regular.otf python3 firmware/scripts/gen_font.py

Characters come from three places:

- host/, hooks/, firmware/src, and firmware/include (the on-screen UI copy)
- TITLE_SEED below (the known sidebar title, plus a few task-title words)
- common_han_3500.txt, the 1988 现代汉语常用字表 (2500 + 1000)

Sidebar titles arrive at runtime from ~/.codex/session_index.jsonl and are not
in the source tree. Do not read that file into this generator: it would bake
private thread names into the repo. The common-character table covers almost
all of those titles, including 查明 10 处模型跳变. A genuinely rare character
can still be added to TITLE_SEED, then regenerate:

    FONT_PATH=/tmp/fonts/NotoSansSC-Regular.otf python3 firmware/scripts/gen_font.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT_H = ROOT / "firmware" / "src" / "font_zh.h"
OUT_CPP = ROOT / "firmware" / "src" / "font_zh.cpp"
COMMON_HAN = HERE / "common_han_3500.txt"
SCALE = 3

CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    "/tmp/fonts/NotoSansSC-Regular.otf",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]

# Glyphs for dynamic Codex sidebar titles. The first sentence is a real
# title the board must render. The rest are common title words so the next
# short name is less likely to show missing boxes. Digits and ASCII
# parentheses are already included via the basic Latin set.
TITLE_SEED = """
查明 10 处模型跳变
查明 10 处模型跳变 (2)
修复实现添加删除更新检查测试优化调整整理补充完善排查解决处理分析对比验证确认
取消保存打开关闭显示隐藏连接断开失败成功错误警告提示输入输出
文件目录路径配置参数环境依赖版本发布部署构建编译运行调试日志记录跟踪监控
性能内存线程进程网络请求响应超时重试缓存数据查询插入修改接口协议格式
解析序列化压缩加密签名权限认证登录注册用户密码令牌会话窗口页面按钮菜单
列表表格图表进度摘要标题内容状态结果原因方案步骤说明文档注释示例样例
预览截图录屏动画字体颜色布局对齐间距边框阴影圆角滚动分页轮播旋转
完成中断等待思考编辑搜索生成图片命令额度上下文电量电池充电无线
崩溃异常堆栈回归兼容迁移回滚合并冲突分支提交推送拉取克隆克隆仓库
设计审查覆盖率单元集成端到端快照基准火焰图瓶颈泄漏抖动卡顿延迟吞吐
重构重命名抽取内联删除死代码补齐缺漏对齐接口契约枚举常量宏模板泛型
异步回调队列锁竞争死锁竞态条件幂等重放去重限流熔断降级扩容缩容
模型推理训练微调量化蒸馏提示词词元采样温度上下文窗口幻觉对齐评测
跳变漂移偏移缩放归一化特征嵌入向量矩阵卷积注意力解码编码
查明核对定位复现根因修复验证回归发布说明变更记录问题单缺陷改进
"""


def find_font() -> str:
    for candidate in CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("找不到中文字体。把 NotoSansSC-Regular.otf 放到 /tmp/fonts/ 或设置 FONT_PATH。")


def collect_codepoints() -> list[int]:
    chars = set(range(32, 127))
    chars.add(0x00B7)  # ·
    chars.add(0x2026)  # …
    chars.add(0xFF08)  # （
    chars.add(0xFF09)  # ）
    for char in TITLE_SEED:
        if ord(char) > 127:
            chars.add(ord(char))
    if not COMMON_HAN.is_file():
        raise SystemExit(f"缺少常用字表 {COMMON_HAN}")
    for char in COMMON_HAN.read_text(encoding="utf-8"):
        if ord(char) > 127:
            chars.add(ord(char))
    roots = [
        ROOT / "host",
        ROOT / "hooks",
        ROOT / "firmware" / "src",
        ROOT / "firmware" / "include",
    ]
    for base in roots:
        for path in base.rglob("*"):
            if path.suffix.lower() not in {".py", ".h", ".cpp"}:
                continue
            if path.name in {"font_zh.cpp", "font_zh.h"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for char in text:
                if ord(char) > 127:
                    chars.add(ord(char))
    return sorted(chars)


def pack_nibbles(levels: list[int]) -> bytes:
    out = bytearray()
    high = None
    for level in levels:
        level &= 0xF
        if high is None:
            high = level
        else:
            out.append((high << 4) | level)
            high = None
    if high is not None:
        out.append(high << 4)
    return bytes(out)


def rasterize(font_path: str, size: int, codepoints: list[int]) -> tuple[list[dict], bytes, int]:
    face = ImageFont.truetype(font_path, size * SCALE)
    glyphs: list[dict] = []
    blob = bytearray()
    line_height = size
    for cp in codepoints:
        char = chr(cp)
        advance = max(1, int(round(face.getlength(char) / SCALE)))
        if char == " ":
            glyphs.append({"cp": cp, "w": 0, "h": 0, "adv": advance, "x": 0, "y": 0, "offset": len(blob)})
            continue
        image = Image.new("L", (size * SCALE * 3, (size + 4) * SCALE), 0)
        ImageDraw.Draw(image).text((0, 0), char, font=face, fill=255)
        bbox = image.getbbox()
        if not bbox:
            glyphs.append({"cp": cp, "w": 0, "h": 0, "adv": advance, "x": 0, "y": 0, "offset": len(blob)})
            continue
        crop = image.crop(bbox).resize(
            (max(1, int(round((bbox[2] - bbox[0]) / SCALE))), max(1, int(round((bbox[3] - bbox[1]) / SCALE)))),
            Image.Resampling.LANCZOS,
        )
        levels: list[int] = []
        for y in range(crop.height):
            for x in range(crop.width):
                alpha = crop.getpixel((x, y)) / 255.0
                levels.append(int(round((alpha**0.72) * 15)))
        packed = pack_nibbles(levels)
        glyph = {
            "cp": cp,
            "w": crop.width,
            "h": crop.height,
            "adv": max(advance, 1),
            "x": int(round(bbox[0] / SCALE)),
            "y": int(round(bbox[1] / SCALE)),
            "offset": len(blob),
        }
        if glyph["w"] > 255 or glyph["h"] > 255 or glyph["adv"] > 255:
            raise SystemExit(f"字形过大 U+{cp:04X}")
        glyphs.append(glyph)
        blob.extend(packed)
        line_height = max(line_height, glyph["y"] + glyph["h"])
    return glyphs, bytes(blob), line_height


def write_output(fonts: dict[str, tuple[list[dict], bytes, int]], font_path: str) -> None:
    lines = [
        "#pragma once",
        "#include \"font.h\"",
        "// Generated by firmware/scripts/gen_font.py",
        f"// Source face: {font_path}",
        "// Glyphs are a subset of Noto Sans CJK SC, SIL Open Font License 1.1.",
        "",
    ]
    OUT_H.write_text("\n".join(lines), encoding="utf-8")

    body = [
        "#include \"font_zh.h\"",
        "",
        "// Rasterized subset of Noto Sans CJK SC Regular (SIL OFL 1.1).",
        "// See firmware/fonts/OFL.txt and firmware/fonts/NOTICE.",
        "",
    ]
    for name, (glyphs, blob, line_height) in fonts.items():
        body.append(f"static const uint8_t {name}_BITMAP[] = {{")
        for index in range(0, len(blob), 20):
            chunk = ", ".join(f"0x{byte:02x}" for byte in blob[index : index + 20])
            body.append(f"  {chunk},")
        if not blob:
            body.append("  0x00,")
        body.append("};")
        body.append(f"static const Glyph {name}_GLYPHS[] = {{")
        for glyph in glyphs:
            body.append(
                "  {0x%04X, %d, %d, %d, %d, %d, %u},"
                % (glyph["cp"], glyph["w"], glyph["h"], glyph["adv"], glyph["x"], glyph["y"], glyph["offset"])
            )
        body.append("};")
        body.append(
            f"const Font {name} = {{{line_height}, {len(glyphs)}, {name}_GLYPHS, {name}_BITMAP}};"
        )
        body.append("")
    OUT_CPP.write_text("\n".join(body), encoding="utf-8")


def main() -> None:
    font_path = find_font()
    codepoints = collect_codepoints()
    numeric = [cp for cp in codepoints if chr(cp) in " 0123456789%.-"]
    fonts = {
        "FONT_UI": rasterize(font_path, 16, codepoints),
        "FONT_SUB": rasterize(font_path, 12, codepoints),
        "FONT_NUM": rasterize(font_path, 18, numeric),
    }
    write_output(fonts, font_path)
    sample = "执行中"
    ui = {glyph["cp"]: glyph for glyph in fonts["FONT_UI"][0]}
    width = sum(ui[ord(char)]["adv"] for char in sample)
    print(f"字体 {font_path}")
    print(f"UI {len(fonts['FONT_UI'][0])} 字, 行高 {fonts['FONT_UI'][2]}, “{sample}” 宽 {width}px")
    print(f"写入 {OUT_CPP} ({OUT_CPP.stat().st_size} 字节)")


if __name__ == "__main__":
    main()
