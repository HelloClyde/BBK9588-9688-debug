/* SPDX-License-Identifier: GPL-3.0-only
 * User-authorized physical recording probe for the resident EXEC facility.
 * Keep this exact loaded image until safe_to_unload is 1. No GUI is invoked.
 */
#include "../module_abi.h"
#include "bda_9688_recording.h"
#include "bda_filesystem.h"

enum {
    RECORD_DETECT = 0, RECORD_BEGIN = 1, RECORD_STATUS = 2,
    RECORD_STOP = 3, RECORD_FINISH = 4, RECORD_FILE_OPEN = 5,
    RECORD_FILE_READ = 6, RECORD_FILE_CLOSE = 7, RECORD_CLEANUP = 8,
    RECORD_READ_EXISTING = 9, RECORD_FS_DIAGNOSTICS = 10
};

/* Every result begins with these 20 little-endian u32 words (80 bytes):
 * version, opcode, signed_rc, recorder_state, native_state, elapsed_ms,
 * native_error, handle, completed, initialized, safe_to_unload,
 * file_handle, file_size, file_offset, data_bytes, format_tag,
 * bits_per_sample, sample_rate, channels, maximum_observed_elapsed_ms.
 * FILE_READ appends data_bytes raw file bytes; at most 432 bytes per call.
 * FS_DIAGNOSTICS instead appends ten raw u32 values after the header:
 * stage, fopen, seek_end, tell_end, seek_start, tell_start,
 * last_seek, last_fread, last_fclose, tell_after_read. An uncalled value is
 * 0x80000000. READ_EXISTING accepts the same root 8.3 path as BEGIN but opens
 * only rb; it never starts audio or creates/truncates/deletes a file.
 */
#define PROBE_HEADER_WORDS 20u
#define PROBE_HEADER_BYTES (PROBE_HEADER_WORDS * 4u)
#define PROBE_FILE_LIMIT (2u * 1024u * 1024u)

static bda_9688_context_t platform;
static bda_9688_recorder_t recorder;
static u32 initialized, last_native_state, last_elapsed_ms, maximum_elapsed_ms;
static int file_handle;
static u32 file_size, file_offset, path_owned;
static char output_path[16];
static u32 fs_diagnostics[10];

static int recorder_quiet(void) {
    return !recorder.handle && !bda_9688_recording_registry.owner &&
        !bda_9688_recording_registry.handle;
}

static void put32(u8 *where, u32 value) {
    for (unsigned i = 0; i < 4u; ++i) where[i] = (u8)(value >> (8u*i));
}

static u32 get32(const u8 *where) {
    return where[0] | ((u32)where[1] << 8) | ((u32)where[2] << 16) | ((u32)where[3] << 24);
}

static int fs_open(const char *path) {
    typedef int (*function)(const char *, const char *);
    return ((function)bda_sdk_internal_api((void *)platform.runtime_tables[1], BDA_SDK_INTERNAL_FS_OPEN))(path, "rb");
}

static int fs_close(int file) {
    typedef int (*function)(int);
    return ((function)bda_sdk_internal_api((void *)platform.runtime_tables[1], BDA_SDK_INTERNAL_FS_CLOSE))(file);
}

static int fs_seek(int file, s32 offset, int whence) {
    typedef int (*function)(int, s32, int);
    return ((function)bda_sdk_internal_api((void *)platform.runtime_tables[1], BDA_SDK_INTERNAL_FS_SEEK))(file, offset, whence);
}

static int fs_tell(int file) {
    typedef int (*function)(int);
    return ((function)bda_sdk_internal_api((void *)platform.runtime_tables[1], BDA_SDK_INTERNAL_FS_TELL))(file);
}

static int fs_read(void *destination, u32 count, int file) {
    typedef int (*function)(void *, u32, u32, int);
    return ((function)bda_sdk_internal_api((void *)platform.runtime_tables[1], BDA_SDK_INTERNAL_FS_READ))(destination, 1u, count, file);
}

static int detect(const debug9688_api *api) {
    if (initialized) return BDA_9688_OK;
    int result = bda_9688_probe(&platform);
    if (result) return result;
    for (unsigned i = 0; i < 5u; ++i)
        if (platform.runtime_tables[i] != api->runtime_table(i)) return BDA_9688_E_UNSUPPORTED;
    result = bda_9688_recorder_init(&recorder, &platform);
    if (!result) initialized = 1u;
    return result;
}

/* Only fresh root-level A:\<1..8 ASCII name characters>.WAV files.
 * This prevents accidental truncation of any caller-supplied arbitrary path.
 * BEGIN args may include one terminating NUL, otherwise are length-delimited.
 */
static int copy_path(const u8 *args, u32 length) {
    if (length && !args[length-1u]) --length;
    if (length < 8u || length > 15u || args[0] != 'A' || args[1] != ':' || args[2] != '\\')
        return BDA_9688_E_ARGUMENT;
    if (args[length-4u] != '.' || args[length-3u] != 'W' ||
        args[length-2u] != 'A' || args[length-1u] != 'V') return BDA_9688_E_ARGUMENT;
    for (u32 i = 3u; i < length-4u; ++i)
        if (!((args[i] >= 'A' && args[i] <= 'Z') ||
              (args[i] >= '0' && args[i] <= '9') || args[i] == '_')) return BDA_9688_E_ARGUMENT;
    for (u32 i = 0; i < length; ++i) output_path[i] = (char)args[i];
    output_path[length] = 0;
    return BDA_9688_OK;
}

static int poll_recorder(void) {
    if (!initialized) return BDA_9688_E_STATE;
    if (!bda_9688_recording_owned(&recorder))
        return recorder_quiet() ? BDA_9688_OK : BDA_9688_E_STATE;
    bda_9688_recording_status_t status;
    int result = bda_9688_recorder_poll(&recorder, &status);
    if (!result) {
        last_native_state = status.native_state;
        last_elapsed_ms = status.elapsed_ms;
        if (status.native_state == 2u && status.elapsed_ms > maximum_elapsed_ms)
            maximum_elapsed_ms = status.elapsed_ms;
    }
    return result;
}

static int close_file(void) {
    if (!file_handle) return BDA_9688_OK;
    int file = file_handle;
    file_handle = 0; /* A consumed native FILE is never closed a second time. */
    int result = fs_close(file);
    fs_diagnostics[8] = (u32)result;
    return result == 0 ? BDA_9688_OK : BDA_9688_E_IO;
}

static int open_read_file(void) {
    for (unsigned i = 0; i < 10u; ++i) fs_diagnostics[i] = 0x80000000u;
    fs_diagnostics[0] = 1u;
    file_handle = fs_open(output_path);
    fs_diagnostics[1] = (u32)file_handle;
    if (!file_handle) return BDA_9688_E_IO;
    fs_diagnostics[0] = 2u;
    int end_result = fs_seek(file_handle, 0, BDA_SEEK_END);
    fs_diagnostics[2] = (u32)end_result;
    fs_diagnostics[0] = 3u;
    int length = fs_tell(file_handle);
    fs_diagnostics[3] = (u32)length;
    /* This native fseek returns the resulting byte position, not C stdio's
     * zero-success convention: 4740 ROM 8017a684 loads FILE+0x44 into its
     * return path at 8017a534. Thus seeking to EOF normally returns size. */
    if (end_result < 0 || length < 0 || length != end_result ||
        (u32)length > PROBE_FILE_LIMIT) {
        (void)close_file();
        return BDA_9688_E_IO;
    }
    fs_diagnostics[0] = 4u;
    int start_result = fs_seek(file_handle, 0, BDA_SEEK_SET);
    fs_diagnostics[4] = (u32)start_result;
    fs_diagnostics[0] = 5u;
    int position = fs_tell(file_handle);
    fs_diagnostics[5] = (u32)position;
    if (start_result != 0 || position != 0) {
        (void)close_file();
        return BDA_9688_E_IO;
    }
    file_size = (u32)length;
    file_offset = 0;
    return BDA_9688_OK;
}

static int operate(const debug9688_api *api, debug9688_call *call, u32 *data_bytes) {
    *data_bytes = 0;
    if (call->opcode == RECORD_DETECT) return detect(api);
    if (!initialized) {
        int result = detect(api);
        if (result) return result;
    }
    switch (call->opcode) {
    case RECORD_BEGIN: {
        if (!recorder_quiet() || file_handle) return BDA_9688_E_BUSY;
        path_owned = 0;
        int result = copy_path(call->args, call->args_size);
        if (result) return result;
        int existing = fs_open(output_path);
        if (existing) {
            (void)fs_close(existing);
            return BDA_9688_E_BUSY; /* Never pass an existing file to native wb. */
        }
        result = bda_9688_recorder_open(&recorder, output_path);
        if (result) return result;
        path_owned = 1;
        last_native_state = last_elapsed_ms = maximum_elapsed_ms = 0;
        file_size = file_offset = 0;
        result = bda_9688_recorder_start(&recorder);
        if (result && recorder.state == BDA_9688_REC_OPEN && !recorder.completed) {
            int finish_result = bda_9688_recorder_finish(&recorder);
            if (finish_result) return finish_result;
        }
        return result;
    }
    case RECORD_STATUS:
        return poll_recorder();
    case RECORD_STOP:
        return bda_9688_recorder_stop(&recorder);
    case RECORD_FINISH:
        return bda_9688_recorder_finish(&recorder);
    case RECORD_FILE_OPEN: {
        if (!recorder_quiet() || file_handle || !path_owned ||
            recorder.state != BDA_9688_REC_CLOSED || !output_path[0]) return BDA_9688_E_STATE;
        return open_read_file();
    }
    case RECORD_FILE_READ: {
        if (!file_handle || call->args_size != 8u) return BDA_9688_E_STATE;
        u32 offset = get32(call->args), wanted = get32(call->args+4u);
        if (offset > file_size || !wanted || wanted > 432u ||
            wanted > call->output_capacity-PROBE_HEADER_BYTES) return BDA_9688_E_RANGE;
        if (wanted > file_size-offset) wanted = file_size-offset;
        fs_diagnostics[0] = 6u;
        int seek_result = fs_seek(file_handle, (s32)offset, BDA_SEEK_SET);
        fs_diagnostics[6] = (u32)seek_result;
        if (seek_result != (s32)offset) return BDA_9688_E_IO;
        int count = fs_read(call->output+PROBE_HEADER_BYTES, wanted, file_handle);
        fs_diagnostics[7] = (u32)count;
        fs_diagnostics[9] = (u32)fs_tell(file_handle);
        if (count < 0 || (u32)count != wanted) return BDA_9688_E_IO;
        file_offset = offset+(u32)count;
        *data_bytes = (u32)count;
        return BDA_9688_OK;
    }
    case RECORD_FILE_CLOSE:
        return close_file();
    case RECORD_READ_EXISTING: {
        if (!recorder_quiet() || file_handle) return BDA_9688_E_BUSY;
        path_owned = 0;
        int result = copy_path(call->args, call->args_size);
        if (result) return result;
        result = open_read_file();
        if (!result) path_owned = 1u;
        return result;
    }
    case RECORD_FS_DIAGNOSTICS:
        if (call->output_capacity < PROBE_HEADER_BYTES+40u) return BDA_9688_E_RANGE;
        for (unsigned i = 0; i < 10u; ++i)
            put32(call->output+PROBE_HEADER_BYTES+i*4u, fs_diagnostics[i]);
        return BDA_9688_OK;
    case RECORD_CLEANUP: {
        if (bda_9688_recording_owned(&recorder)) {
            if (recorder.completed || recorder.state == BDA_9688_REC_OPEN) {
                int result = bda_9688_recorder_finish(&recorder);
                if (result) return result;
            } else {
                /* Request stop when active, then keep the image resident for
                 * later STATUS/CLEANUP calls until its callback has returned. */
                int result = bda_9688_recorder_stop(&recorder);
                return result ? result : BDA_9688_E_BUSY;
            }
        }
        if (!recorder_quiet()) return BDA_9688_E_STATE;
        return close_file();
    }
    default:
        return BDA_9688_E_ARGUMENT;
    }
}

int debug9688_entry(const debug9688_api *api, debug9688_call *call) {
    if (!api || api->version != 1u || !call || call->output_capacity < PROBE_HEADER_BYTES)
        return BDA_9688_E_ARGUMENT;
    u32 data_bytes = 0;
    int result = operate(api, call, &data_bytes);
    /* No wait loop here: returning lets the debugger's worker yield normally
     * to native audio tasks. Pending callbacks retain this exact module text. */
    const u32 values[PROBE_HEADER_WORDS] = {
        1u, call->opcode, (u32)result, recorder.state, last_native_state,
        last_elapsed_ms, recorder.native_error, recorder.handle, recorder.completed,
        initialized, recorder_quiet() && !file_handle,
        (u32)file_handle, file_size, file_offset, data_bytes,
        recorder.format.format_tag, recorder.format.bits_per_sample,
        recorder.format.sample_rate, recorder.format.channels, maximum_elapsed_ms
    };
    for (unsigned i = 0; i < PROBE_HEADER_WORDS; ++i) put32(call->output+i*4u, values[i]);
    call->output_size = PROBE_HEADER_BYTES+data_bytes;
    if (!result && call->opcode == RECORD_FS_DIAGNOSTICS) call->output_size += 40u;
    return result;
}
