#pragma once

void netBegin();
// True when a poll or a link change may have altered what the screen shows.
bool netLoop(bool force);
