/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef DEBUG9588_SCREEN_H
#define DEBUG9588_SCREEN_H
#include <stdint.h>
#define RD_SCREEN_BYTES (FW_SCREEN_WIDTH*FW_SCREEN_HEIGHT*4u)
int rd_screen_capture_area(uint8_t *,uint32_t,uint32_t metadata[6],const uint32_t rect[4]);
int rd_screen_capture(uint8_t *,uint32_t,uint32_t metadata[6]);
#endif
