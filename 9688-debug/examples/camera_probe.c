/* SPDX-License-Identifier: GPL-3.0-only
 * Explicitly invoked hardware probe for Debug9688 EXEC, not a BDA entry.
 * Keep this module installed until STATE reports CLOSED and both copy
 * pointers are zero. FREE/CLOSE must invoke the same image using RPC 30;
 * Client.exec_run() uploads a new image and must not be used for cleanup.
 * Opcodes: 0 STATE, 1 CAPTURE, 2 CONVERT, 3 FREE, 4 CLOSE, 5 FRAME_STATS.
 * CAPTURE accepts zero args (120 native ticks, no discarded frames), one
 * LE u32 timeout in 1..300, or two LE u32 {timeout,warmup_frames} with 0..24
 * warmup frames discarded before copying the final frame. For first sensor
 * initialization, {300,12} captures 13 real frames in the same open session.
 * This follows the stock capture path's 13-frame counter without inheriting
 * its uncleared done flag. Each SDK start clears done and each poll waits for
 * disabled DMA. Use EXEC budget 400 and keep one serial session throughout.
 * Result is 32 little-endian u32 words (signed RC fields use two's complement):
 *  0 version, 1 reply magic, 2 opcode, 3 operation RC, 4 stage, 5 chip,
 *  6 camera state, 7 raw pointer, 8 raw bytes, 9 RGB565 pointer, 10 RGB bytes,
 * 11 width, 12 height, 13 raw stride, 14 raw format, 15 start tick,
 * 16 end tick, 17 cancel flags, 18 platform RC, 19 init RC, 20 open RC,
 * 21 start RC, 22 poll RC, 23 close RC, 24 convert RC, 25 copied bytes,
 * 26 native opened, 27 native device, 28 native done, 29 native UI state,
 * 30 current CIM IRQ handler, 31 CPM CLKGR.
 * FRAME_STATS alone appends 4 more u32 words: requested warmup frames,
 * actual completed frames, first frame sampled luma sum, final frame sampled
 * luma sum. Each sum samples 256 evenly spaced Y bytes; divide by 256 for
 * sampled mean luma. The first 32 fields retain exactly the same meanings.
 * A downloadable frame requires operation RC=0, camera state=CLOSED(0),
 * close RC=0 and the corresponding copy byte count=153600. Native fault or
 * incomplete close means keep this image/context installed; never force-free
 * another owner's allocation or replace code while an IRQ may reference it.
 */
#include "module_abi.h"
#include "bda_9688_camera.h"

enum { PROBE_STATE = 0, PROBE_CAPTURE = 1, PROBE_CONVERT = 2,
       PROBE_FREE = 3, PROBE_CLOSE = 4, PROBE_FRAME_STATS = 5 };
enum { ST_IDLE, ST_PLATFORM, ST_ALLOCATE, ST_OPEN, ST_START, ST_POLL,
       ST_COPY, ST_CLOSE, ST_READY, ST_CONVERT, ST_FREED };
#define PROBE_REPLY_WORDS 32u
#define PROBE_REPLY_MAGIC 0x434c5031u
#define PROBE_CANCELLED (-8)

static bda_9688_context_t platform;
static bda_9688_camera_t camera;
static u8 *raw_copy;
static u16 *rgb_copy;
static u32 raw_bytes, rgb_bytes, stage, began, ended, cancelled, copied;
static u32 warmup_requested, frames_completed, first_luma_sum, last_luma_sum;
static int probe_result, init_result, open_result, start_result;
static int poll_result, close_result, convert_result;

static u32 get32(const u8 *p) {
    return (u32)p[0] | ((u32)p[1] << 8) | ((u32)p[2] << 16) | ((u32)p[3] << 24);
}
static void put32(u8 *p, u32 value) {
    for (unsigned i = 0; i < 4u; ++i) p[i] = (u8)(value >> (8u * i));
}
static int prepare(const debug9688_api *api) {
    if (camera.magic != BDA_9688_CAMERA_MAGIC) stage = ST_PLATFORM;
    probe_result = bda_9688_probe(&platform);
    if (probe_result) return probe_result;
    for (unsigned i = 0; i < 5u; ++i)
        if (api->runtime_table(i) != platform.runtime_tables[i])
            return BDA_9688_E_UNSUPPORTED;
    if (camera.magic != BDA_9688_CAMERA_MAGIC) {
        bda_9688_camera_zero((volatile u32 *)&camera, sizeof(camera) / sizeof(u32));
        init_result = bda_9688_camera_init(&camera, &platform);
        if (init_result) return init_result;
    }
    return BDA_9688_OK;
}
static void *allocate(u32 bytes) {
    return (void *)(u32)bda_sdk_internal_call1((void *)platform.runtime_tables[3], BDA_SDK_INTERNAL_MEM_ALLOC, bytes);
}
static void release(void *pointer) {
    (void)bda_sdk_internal_call1((void *)platform.runtime_tables[3], BDA_SDK_INTERNAL_MEM_FREE, (u32)pointer);
}
static int camera_close(void) {
    if (camera.magic != BDA_9688_CAMERA_MAGIC) return BDA_9688_OK;
    stage = ST_CLOSE;
    close_result = bda_9688_camera_close(&camera);
    return close_result;
}
static int capture(const debug9688_api *api, u32 timeout_ticks, u32 warmup_frames) {
    if (raw_copy || rgb_copy || camera.state != BDA_9688_CAMERA_CLOSED)
        return BDA_9688_E_BUSY;
    raw_bytes = rgb_bytes = copied = cancelled = 0;
    warmup_requested = warmup_frames;
    frames_completed = first_luma_sum = last_luma_sum = 0;
    open_result = start_result = poll_result = close_result = convert_result = 0;
    began = api->tick();
    stage = ST_ALLOCATE;
    raw_copy = (u8 *)allocate(BDA_9688_CAMERA_BYTES);
    if (!raw_copy) return BDA_9688_E_NATIVE;
    /* Retain unexpected returned storage rather than freeing a guessed
     * address. A correct native global allocator always meets this range. */
    if (!bda_9688_camera_ram((u32)raw_copy, BDA_9688_CAMERA_BYTES))
        return BDA_9688_E_RANGE;
    stage = ST_OPEN;
    open_result = bda_9688_camera_open_qvga(&camera);
    int result = open_result;
    for (u32 frame_index = 0; !result && frame_index <= warmup_frames; ++frame_index) {
        if (frame_index) {
            /* Leave time for sensor AE/AWB and the RTOS between captures.
             * Only the SDK's checked native init table configures the sensor. */
            for (unsigned spacing = 0; spacing < 2u; ++spacing) {
                cancelled = (u32)api->poll_cancel();
                if (cancelled) { result = PROBE_CANCELLED; break; }
                if (api->tick() - began >= timeout_ticks) {
                    result = BDA_9688_E_BUSY;
                    break;
                }
            }
            if (result) break;
        }
        stage = ST_START;
        start_result = bda_9688_camera_start(&camera);
        result = start_result;
        if (result) break;
        bda_9688_camera_frame_t frame;
        stage = ST_POLL;
        poll_result = BDA_9688_E_BUSY;
        for (unsigned attempts = 0; attempts < 400u; ++attempts) {
            poll_result = bda_9688_camera_poll(&camera, &frame);
            if (poll_result != BDA_9688_E_BUSY) break;
            cancelled = (u32)api->poll_cancel();
            if (cancelled || api->tick() - began >= timeout_ticks) break;
        }
        result = cancelled ? PROBE_CANCELLED : poll_result;
        if (!result) {
            if (frame.width != 320u || frame.height != 240u || frame.stride != 640u ||
                frame.bytes != BDA_9688_CAMERA_BYTES || frame.format != BDA_9688_CAMERA_YUYV) {
                result = BDA_9688_E_NATIVE;
            } else {
                ++frames_completed;
                u32 luma_sum = 0;
                const volatile u8 *luma = (const volatile u8 *)frame.pixels;
                for (u32 sample = 0; sample < 256u; ++sample)
                    luma_sum += luma[(sample * BDA_9688_CAMERA_BYTES / 256u) & ~1u];
                if (frames_completed == 1u) first_luma_sum = luma_sum;
                last_luma_sum = luma_sum;
                if (frame_index < warmup_frames) continue;
                stage = ST_COPY;
                const volatile u32 *source = (const volatile u32 *)frame.pixels;
                u32 *destination = (u32 *)raw_copy;
                for (u32 i = 0; i < BDA_9688_CAMERA_BYTES / 4u; ++i) {
                    destination[i] = source[i];
                    copied = 4u * (i + 1u);
                    if ((i & 4095u) == 4095u) {
                        cancelled = (u32)api->poll_cancel();
                        if (cancelled) { result = PROBE_CANCELLED; break; }
                    }
                }
                if (!result) raw_bytes = BDA_9688_CAMERA_BYTES;
            }
        }
    }
    /* A borrowed native frame is never published. No frame is downloadable
     * until its own copy is complete and this close returns successfully. */
    int closing = camera_close();
    if (closing) result = closing;
    if (!closing && result) {
        release(raw_copy);
        raw_copy = (u8 *)0;
        raw_bytes = 0;
    }
    if (!result) stage = ST_READY;
    ended = api->tick();
    return result;
}
static int convert(void) {
    if (camera.state != BDA_9688_CAMERA_CLOSED || close_result ||
        !raw_copy || raw_bytes != BDA_9688_CAMERA_BYTES)
        return BDA_9688_E_STATE;
    if (rgb_copy) return BDA_9688_E_BUSY;
    stage = ST_CONVERT;
    rgb_copy = (u16 *)allocate(BDA_9688_CAMERA_BYTES);
    if (!rgb_copy) return BDA_9688_E_NATIVE;
    if (!bda_9688_camera_ram((u32)rgb_copy, BDA_9688_CAMERA_BYTES))
        return BDA_9688_E_RANGE;
    if ((u32)rgb_copy < (u32)raw_copy + BDA_9688_CAMERA_BYTES &&
        (u32)raw_copy < (u32)rgb_copy + BDA_9688_CAMERA_BYTES)
        return BDA_9688_E_NATIVE;
    convert_result = bda_9688_camera_yuyv_to_rgb565(raw_copy, raw_bytes,
        rgb_copy, 320u * 240u, 320u, 240u);
    if (convert_result) {
        release(rgb_copy);
        rgb_copy = (u16 *)0;
        rgb_bytes = 0;
        return convert_result;
    }
    rgb_bytes = BDA_9688_CAMERA_BYTES;
    stage = ST_READY;
    return BDA_9688_OK;
}
static int free_copies(void) {
    if (camera.state != BDA_9688_CAMERA_CLOSED) return BDA_9688_E_BUSY;
    if ((raw_copy && !bda_9688_camera_ram((u32)raw_copy, BDA_9688_CAMERA_BYTES)) ||
        (rgb_copy && !bda_9688_camera_ram((u32)rgb_copy, BDA_9688_CAMERA_BYTES)))
        return BDA_9688_E_RANGE;
    if (raw_copy && rgb_copy &&
        (u32)rgb_copy < (u32)raw_copy + BDA_9688_CAMERA_BYTES &&
        (u32)raw_copy < (u32)rgb_copy + BDA_9688_CAMERA_BYTES)
        return BDA_9688_E_RANGE;
    if (rgb_copy) { release(rgb_copy); rgb_copy = (u16 *)0; }
    if (raw_copy) { release(raw_copy); raw_copy = (u8 *)0; }
    raw_bytes = rgb_bytes = 0;
    stage = ST_FREED;
    return BDA_9688_OK;
}
static void reply(debug9688_call *call, int result) {
    const bda_9688_camera_rom_t *rom = camera.rom;
    const u32 values[PROBE_REPLY_WORDS] = {
        1u, PROBE_REPLY_MAGIC, call->opcode, (u32)result, stage,
        platform.profile ? (u32)platform.profile->chip : 0u,
        camera.state, (u32)raw_copy, raw_bytes, (u32)rgb_copy, rgb_bytes,
        320u, 240u, 640u, BDA_9688_CAMERA_YUYV, began, ended, cancelled,
        (u32)probe_result, (u32)init_result, (u32)open_result, (u32)start_result,
        (u32)poll_result, (u32)close_result, (u32)convert_result, copied,
        rom ? bda_9688_camera_read(rom->opened) : 0u,
        rom ? bda_9688_camera_read(rom->device) : 0u,
        rom ? bda_9688_camera_read(rom->done) : 0u,
        rom ? bda_9688_camera_read(rom->native_ui_state) : 0u,
        rom ? bda_9688_camera_read(rom->irq_slot) : 0u,
        rom ? bda_9688_camera_read(0xb0000020u) : 0u
    };
    for (unsigned i = 0; i < PROBE_REPLY_WORDS; ++i)
        put32(call->output + 4u * i, values[i]);
    call->output_size = PROBE_REPLY_WORDS * 4u;
    if (call->opcode == PROBE_FRAME_STATS && call->output_capacity >= 144u) {
        put32(call->output + 128u, warmup_requested);
        put32(call->output + 132u, frames_completed);
        put32(call->output + 136u, first_luma_sum);
        put32(call->output + 140u, last_luma_sum);
        call->output_size = 144u;
    }
}

int debug9688_entry(const debug9688_api *api, debug9688_call *call) {
    if (!api || api->version != 1u || api->size < sizeof(*api) ||
        !api->tick || !api->poll_cancel || !api->runtime_table || !call ||
        call->output_capacity < PROBE_REPLY_WORDS * 4u)
        return BDA_9688_E_ARGUMENT;
    int result = prepare(api);
    if (!result) {
        switch (call->opcode) {
        case PROBE_STATE: break;
        case PROBE_CAPTURE: {
            u32 timeout = 120u, warmup = 0;
            if (call->args_size == 8u) {
                timeout = get32(call->args);
                warmup = get32(call->args + 4u);
            }
            else if (call->args_size == 4u) timeout = get32(call->args);
            else if (call->args_size) { result = BDA_9688_E_ARGUMENT; break; }
            if (!timeout || timeout > 300u || warmup > 24u) { result = BDA_9688_E_ARGUMENT; break; }
            result = capture(api, timeout, warmup);
            break;
        }
        case PROBE_CONVERT: result = convert(); break;
        case PROBE_FREE: result = free_copies(); break;
        case PROBE_CLOSE: result = camera_close(); break;
        case PROBE_FRAME_STATS:
            if (call->output_capacity < 144u) result = BDA_9688_E_ARGUMENT;
            break;
        default: result = BDA_9688_E_ARGUMENT; break;
        }
    }
    reply(call, result);
    return result;
}
