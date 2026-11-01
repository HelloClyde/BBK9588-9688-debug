/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef DEBUG9588_PROTOCOL_H
#define DEBUG9588_PROTOCOL_H
#include <stdint.h>
#define DEBUG_MAX_PAYLOAD 512u
typedef struct {uint8_t rx[528],tx[528];unsigned rx_count,tx_size,tx_offset;uint32_t bad_frames;} debug_protocol;
typedef unsigned (*debug_command_fn)(unsigned,const uint8_t *,unsigned,uint8_t *,unsigned *);
void debug_protocol_feed(debug_protocol *,const uint8_t *,unsigned,debug_command_fn);
uint32_t debug_crc32(const uint8_t *,unsigned);
#endif
