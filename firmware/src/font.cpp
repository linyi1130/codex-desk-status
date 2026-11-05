#include "font.h"

#include "font_zh.h"

const Glyph* findGlyph(const Font& font, uint16_t cp) {
  int lo = 0;
  int hi = static_cast<int>(font.count) - 1;
  while (lo <= hi) {
    const int mid = (lo + hi) >> 1;
    const uint16_t found = font.glyphs[mid].cp;
    if (found == cp) {
      return &font.glyphs[mid];
    }
    if (found < cp) {
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return nullptr;
}
