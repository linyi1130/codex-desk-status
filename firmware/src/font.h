#pragma once

#include <stddef.h>
#include <stdint.h>

struct Glyph {
  uint16_t cp;
  uint8_t width;
  uint8_t height;
  uint8_t advance;
  int8_t xOffset;
  int8_t yOffset;
  uint32_t bitmap;
};

struct Font {
  uint8_t lineHeight;
  uint16_t count;
  const Glyph* glyphs;
  const uint8_t* bitmap;
};

extern const Font FONT_UI;
extern const Font FONT_SUB;
extern const Font FONT_NUM;

const Glyph* findGlyph(const Font& font, uint16_t cp);

inline bool nextCodepoint(const char*& text, uint16_t& cp) {
  const uint8_t lead = static_cast<uint8_t>(*text);
  if (lead == 0) {
    return false;
  }
  text++;
  if (lead < 0x80) {
    cp = lead;
    return true;
  }
  if ((lead & 0xE0) == 0xC0 && (static_cast<uint8_t>(*text) & 0xC0) == 0x80) {
    cp = (static_cast<uint16_t>(lead & 0x1F) << 6) | (static_cast<uint8_t>(*text) & 0x3F);
    text++;
    return true;
  }
  if ((lead & 0xF0) == 0xE0 && (static_cast<uint8_t>(text[0]) & 0xC0) == 0x80 &&
      (static_cast<uint8_t>(text[1]) & 0xC0) == 0x80) {
    cp = (static_cast<uint16_t>(lead & 0x0F) << 12) |
         (static_cast<uint16_t>(static_cast<uint8_t>(text[0]) & 0x3F) << 6) |
         (static_cast<uint8_t>(text[1]) & 0x3F);
    text += 2;
    return true;
  }
  cp = '?';
  return true;
}

inline int glyphAdvance(const Font& font, uint16_t cp) {
  const Glyph* glyph = findGlyph(font, cp);
  if (glyph) {
    return glyph->advance;
  }
  return font.lineHeight / 2;
}

inline int textWidth(const Font& font, const char* utf8) {
  int width = 0;
  uint16_t cp = 0;
  while (utf8 && nextCodepoint(utf8, cp)) {
    width += glyphAdvance(font, cp);
  }
  return width;
}

inline uint16_t blend565(uint16_t fg, uint16_t bg, uint8_t alpha) {
  if (alpha >= 15) {
    return fg;
  }
  if (alpha == 0) {
    return bg;
  }
  const uint32_t inverse = 15 - alpha;
  const uint32_t r = (((fg >> 11) & 31) * alpha + ((bg >> 11) & 31) * inverse) / 15;
  const uint32_t g = (((fg >> 5) & 63) * alpha + ((bg >> 5) & 63) * inverse) / 15;
  const uint32_t b = ((fg & 31) * alpha + (bg & 31) * inverse) / 15;
  return static_cast<uint16_t>((r << 11) | (g << 5) | b);
}

template <typename Gfx>
void blitGlyph(Gfx& gfx, const Font& font, const Glyph& glyph, int x, int y, uint16_t color, int maxX, int maxY) {
  const uint8_t* data = font.bitmap + glyph.bitmap;
  int cursor = 0;
  for (int row = 0; row < glyph.height; ++row) {
    for (int col = 0; col < glyph.width; ++col) {
      const uint8_t byte = data[cursor >> 1];
      const uint8_t alpha = (cursor & 1) ? (byte & 0x0F) : (byte >> 4);
      cursor++;
      if (!alpha) {
        continue;
      }
      const int px = x + glyph.xOffset + col;
      const int py = y + glyph.yOffset + row;
      if (px < 0 || py < 0 || px >= maxX || py >= maxY) {
        continue;
      }
      gfx.drawPixel(px, py, blend565(color, gfx.readPixel(px, py), alpha));
    }
  }
}

template <typename Gfx>
int drawText(Gfx& gfx, const Font& font, int x, int y, const char* utf8, uint16_t color, int maxX, int maxY) {
  uint16_t cp = 0;
  while (utf8 && *utf8 && x < maxX) {
    nextCodepoint(utf8, cp);
    const Glyph* glyph = findGlyph(font, cp);
    if (!glyph) {
      const int box = font.lineHeight / 2;
      gfx.drawRect(x, y + 2, box - 2, font.lineHeight - 4, color);
      x += box;
      continue;
    }
    blitGlyph(gfx, font, *glyph, x, y, color, maxX, maxY);
    x += glyph->advance;
  }
  return x;
}

template <typename Gfx>
int drawTextFit(Gfx& gfx, const Font& font, int x, int y, const char* utf8, uint16_t color, int maxX, int maxY) {
  if (!utf8) {
    return x;
  }
  const int limit = maxX - x;
  if (textWidth(font, utf8) <= limit) {
    return drawText(gfx, font, x, y, utf8, color, maxX, maxY);
  }
  const int ellipsis = textWidth(font, "…");
  char buffer[160];
  size_t used = 0;
  const char* cursor = utf8;
  uint16_t cp = 0;
  int width = 0;
  while (*cursor && used + 4 < sizeof(buffer)) {
    const char* before = cursor;
    nextCodepoint(cursor, cp);
    const int step = static_cast<int>(cursor - before);
    const int advance = glyphAdvance(font, cp);
    if (width + advance + ellipsis > limit) {
      break;
    }
    for (int i = 0; i < step; ++i) {
      buffer[used++] = before[i];
    }
    width += advance;
  }
  buffer[used++] = static_cast<char>(0xE2);
  buffer[used++] = static_cast<char>(0x80);
  buffer[used++] = static_cast<char>(0xA6);
  buffer[used] = 0;
  return drawText(gfx, font, x, y, buffer, color, maxX, maxY);
}
