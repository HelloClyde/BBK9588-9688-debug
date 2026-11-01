/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef DEBUG9588_H
#define DEBUG9588_H
#include <stdint.h>
#include <stddef.h>
#define RD_MAGIC 0x42443835u
#define RD_VERSION 2u
#define RD_PRIORITY 6u
#define RD_STACK_BYTES 32768u
enum { RD_INIT, RD_RUNNING, RD_STOPPING, RD_STOPPED, RD_FAULT };
typedef struct rd_header rd_header;
struct rd_header {
    uint32_t magic,version,allocation,base,size,priority;
    volatile uint32_t state,stop_request,last_error;
    int (*init)(rd_header *);
    void (*worker)(void *);
    void (*shutdown)(rd_header *);
    uint32_t stack_top,stack_bottom;
    uint32_t runtime_tables[5];
    uint32_t image_id;
    uint32_t binding_id; /* Core firmware bindings, independent of build ID. */
};
extern rd_header rd_control;
void rd_cache_sync(void *start,void *end);
void *memset(void *dst,int value,size_t size);
void *memcpy(void *dst,const void *src,size_t size);
#endif
