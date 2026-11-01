/* SPDX-License-Identifier: GPL-3.0-only
 * Adapted from H1Debug's heap-resident launcher, 2026-10-31.
 * Ported from 9688 Debug; private bindings require an independently checked 9588 profile.
 */
#include "debug.h"
#include "firmware.h"
#include "resident_image.h"
#include "bda_dialogs.h"

extern unsigned char __bss_start[], __bss_end[];

static int notice(const char *message, int error)
{
    char buffer[240];
    static const char hex[] = "0123456789ABCDEF";
    unsigned n = 0;
    while (message[n] && n < sizeof buffer - 20u) {
        buffer[n] = message[n];
        ++n;
    }
    if (error) {
        buffer[n++] = '\n'; buffer[n++] = '['; buffer[n++] = '0'; buffer[n++] = 'x';
        for (int i = 7; i >= 0; --i) buffer[n++] = hex[((uint32_t)error >> (i * 4)) & 15u];
        buffer[n++] = ']';
    }
    buffer[n] = 0;
    bda_msgbox("9588 Debug", buffer);
    return error;
}

static void sync_code(uint32_t base, uint32_t bytes)
{
    __asm__ volatile("sync" ::: "memory");
    for (uint32_t address = base & ~31u; address < base + bytes; address += 16u)
        __asm__ volatile("cache 0x15,0(%0)" :: "r"(address) : "memory");
    __asm__ volatile("sync" ::: "memory");
    for (uint32_t address = base & ~31u; address < base + bytes; address += 16u)
        __asm__ volatile("cache 0x10,0(%0)" :: "r"(address) : "memory");
    __asm__ volatile("sync\n\tnop\n\tnop\n\tnop" ::: "memory");
}

static int relocate(uint32_t base,const struct rd_image_descriptor *image)
{
    uint32_t delta = base - image->link_base;
    for (unsigned i = 0; i < image->reloc_count; ++i) {
        const struct rd_reloc *rel = &image->relocations[i];
        if ((rel->offset & 3u) || image->image_size < 4u || rel->offset > image->image_size - 4u ||
            rel->value < image->link_base || rel->value - image->link_base > image->memory_size)
            return 0;
        uint32_t *word = (uint32_t *)(uintptr_t)(base + rel->offset);
        uint32_t value = rel->value + delta;
        switch (rel->type) {
        case 1: *word = value; break;
        case 2:
            if ((value & 3u) || ((base + rel->offset + 4u) & 0xf0000000u) != (value & 0xf0000000u))
                return 0;
            *word = (*word & 0xfc000000u) | ((value >> 2) & 0x03ffffffu);
            break;
        case 3: *word = (*word & 0xffff0000u) | (((value + 0x8000u) >> 16) & 0xffffu); break;
        case 4: *word = (*word & 0xffff0000u) | (value & 0xffffu); break;
        default: return 0;
        }
    }
    return 1;
}

static int start(rd_header *control)
{
    control->stop_request = 0;
    control->last_error = 0;
    control->state = RD_INIT;
    int result = control->init(control);
    if (result) {
        control->shutdown(control);
        if (control->state != RD_STOPPED) control->state = RD_FAULT;
        if (!control->last_error) control->last_error = (uint32_t)result;
        return notice("Debug start failed. Unplug USB before retrying.", result);
    }
    control->state = RD_RUNNING;
    return notice("Debug running in background. Return to menu and connect USB.\nOpen this app again to stop.", 0);
}

static int launch(void)
{
    /* Never invoke an unknown GUI table on an unsupported firmware. */
    if (!fw_matches()) {
        if (fw_notice_safe())
            notice("Unsupported firmware or runtime table mismatch.\nA matching checked 9588 JZ4740/JZ4730/JZ4720 profile is required.",
                   (int)fw_match_error());
        return 1;
    }
    const struct rd_image_descriptor *image = 0;
    for(unsigned i=0;i<RD_IMAGES_COUNT;i++)
        if(rd_images[i].soc==fw_selected_soc()){image=&rd_images[i];break;}
    if(!image)return notice("No resident image for detected chip.",11);
#ifdef RD_UNIVERSAL_LAUNCHER
    const char *detected=fw_selected_soc()==4720u ? "Detected JZ4720. Preparing debugger." :
                         fw_selected_soc()==4730u ? "Detected JZ4730. Preparing debugger." :
                                                   "Detected JZ4740. Preparing debugger.";
    notice(detected,0);
#endif
    rd_header *control = fw_find_resident();
    if (control) {
        if (control->size != image->memory_size || control->image_id != image->image_id)
            return notice("Different resident build installed. Restart device before updating.", 2);
        if (control->state == RD_STOPPED) return start(control);
        if (control->state != RD_RUNNING && control->state != RD_STOPPING)
            return notice("Resident is not ready. Restart device before retrying.", 3);
        control->stop_request = 1;
        uint32_t begin = fw_tick();
        for (unsigned i = 0; i < 400u && control->state != RD_STOPPED; ++i) {
            if (fw_tick() - begin > 400u) break;
            fw_delay(1);
        }
        if (control->state != RD_STOPPED)
            return notice("Stop is still pending. Resident memory remains allocated.",
                          (int)(control->last_error ? control->last_error : 4u));
        return notice("Debug stopped. USB restored. Resident allocation is reused until restart.", 0);
    }
    if (!fw_task_slot_empty()) return notice("Debug task priority is occupied.", 5);
    if (image->image_size < sizeof(rd_header) || image->image_size > image->memory_size || image->memory_size > 0x200000u)
        return notice("Invalid resident image.", 6);
    void *allocation = fw_alloc(image->memory_size + 31u);
    if (!allocation) return notice("Not enough memory.", 7);
    uint32_t raw_address = (uint32_t)(uintptr_t)allocation;
    uint32_t base = (raw_address + 31u) & ~31u;
    if (raw_address < RD_FW_SDRAM_START || raw_address > RD_FW_RUNTIME_BASE - (image->memory_size + 31u) ||
        base < raw_address || base > RD_FW_RUNTIME_BASE - image->memory_size) {
        fw_free(allocation);
        return notice("Allocation lies outside the supported RAM window.", 8);
    }
    bda_memset((void *)(uintptr_t)base, 0, image->memory_size);
    bda_memcpy((void *)(uintptr_t)base, image->data, image->image_size);
    if (!relocate(base,image)) {
        fw_free(allocation);
        return notice("Resident relocation failed.", 9);
    }
    control = (rd_header *)(uintptr_t)base;
    if (control->magic != RD_MAGIC || control->version != RD_VERSION || control->image_id != image->image_id) {
        fw_free(allocation);
        return notice("Resident identity mismatch.", 10);
    }
    control->allocation = raw_address;
    control->base = base;
    control->size = image->memory_size;
    control->priority = RD_PRIORITY;
    control->state = RD_INIT;
    control->stop_request = 0;
    control->last_error = 0;
    for (unsigned i = 0; i < 5; ++i)
        control->runtime_tables[i] = *(volatile uint32_t *)(uintptr_t)(RD_FW_RUNTIME_BASE + 4u + i * 4u);
    sync_code(base, image->image_size);
    int result = fw_task_create(control);
    if (result) {
        fw_free(allocation);
        return notice("Unable to create resident task.", result);
    }
    return start(control);
}

__attribute__((section(".text.bda_main")))
int bda_main(void)
{
    /* The native loader does not supply C runtime initialization. */
    for (volatile unsigned char *p = __bss_start; p < __bss_end; ++p) *p = 0;
    return launch();
}
