/* SPDX-License-Identifier: GPL-3.0-only
 * LCD DMA snapshot checks adapted from H1Debug io.c, using 9688 globals. */
#include "firmware.h"
#include "screen.h"
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
static int physical(uint32_t a,uint32_t n)
{return a>=0x4000u && a<(FW_SDRAM_END&0x1fffffffu) && n<=(FW_SDRAM_END&0x1fffffffu)-a;}
int rd_screen_capture(uint8_t *dest,uint32_t capacity,uint32_t m[6])
{
    uint32_t w=R32(FW_LCD_WIDTH),h=R32(FW_LCD_HEIGHT),bits=R32(FW_LCD_BITS);
    if(w!=FW_SCREEN_WIDTH || h!=FW_SCREEN_HEIGHT || !(R32(FW_LCD_BASE+0x30u)&8u))return 2;
    unsigned bytes=bits==16?2u:(bits==18 || bits==32)?4u:0u;
    uint32_t total=w*h*bytes,desc=R32(FW_LCD_BASE+0x40u)&0x1fffffffu;
    if(!bytes || total>capacity || (desc&15u) || !physical(desc,16))return 2;
    uint32_t d=desc|0xa0000000u,next=R32(d),source=R32(d+4),cmd=R32(d+12);
    if((next&0x1fffffffu)!=desc || (cmd&0x10000000u) ||
       (cmd&0x00ffffffu)!=total/4u || (source&3u) || !physical(source,total))return 2;
    const volatile uint32_t *p=(const volatile uint32_t *)(uintptr_t)(source|0xa0000000u);
    for(uint32_t i=0;i<total/4;i++){uint32_t v=p[i];for(unsigned j=0;j<4;j++)dest[4*i+j]=(uint8_t)(v>>(8*j));}
    if((R32(FW_LCD_BASE+0x40u)&0x1fffffffu)!=desc || R32(d+4)!=source || R32(d+12)!=cmd)return 3;
    m[0]=w;m[1]=h;m[2]=w*bytes;m[3]=bytes==2?1u:2u;m[4]=total;m[5]=source|0xa0000000u;return 0;
}
