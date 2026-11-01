/* SPDX-License-Identifier: GPL-3.0-only
 * LCD DMA snapshot checks adapted from H1Debug io.c. */
#include "firmware.h"
#include "screen.h"
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
static int physical(uint32_t a,uint32_t n)
{return a>=0x4000u && a<(FW_SDRAM_END&0x1fffffffu) && n<=(FW_SDRAM_END&0x1fffffffu)-a;}
int rd_screen_capture_area(uint8_t *dest,uint32_t capacity,uint32_t m[6],const uint32_t rect[4])
{
    uint32_t w=R32(FW_LCD_WIDTH),h=R32(FW_LCD_HEIGHT),bits=R32(FW_LCD_BITS);
    if(w!=FW_SCREEN_WIDTH || h!=FW_SCREEN_HEIGHT || !(R32(FW_LCD_BASE+0x30u)&8u))return 2;
    unsigned bytes=bits==16?2u:(bits==18 || bits==32)?4u:0u;
    uint32_t x=0,y=0,cw=w,ch=h;
    if(rect){x=rect[0];y=rect[1];cw=rect[2];ch=rect[3];}
    if(!cw || !ch || x>=w || y>=h || cw>w-x || ch>h-y)return 1;
    uint32_t total=w*h*bytes,cropped=cw*ch*bytes,desc=R32(FW_LCD_BASE+0x40u)&0x1fffffffu;
    if(!bytes || cropped>capacity || (desc&15u) || !physical(desc,16))return 2;
    uint32_t d=desc|0xa0000000u,next=R32(d),source=R32(d+4),cmd=R32(d+12);
    if((next&0x1fffffffu)!=desc || (cmd&0x10000000u) ||
       (cmd&0x00ffffffu)!=total/4u || (source&3u) || !physical(source,total))return 2;
    const volatile uint8_t *p=(const volatile uint8_t *)(uintptr_t)(source|0xa0000000u);
    for(uint32_t row=0;row<ch;row++){
        uint32_t offset=((y+row)*w+x)*bytes;
        for(uint32_t col=0;col<cw*bytes;col++)dest[row*cw*bytes+col]=p[offset+col];
    }
    if((R32(FW_LCD_BASE+0x40u)&0x1fffffffu)!=desc || R32(d+4)!=source || R32(d+12)!=cmd)return 3;
    m[0]=cw;m[1]=ch;m[2]=cw*bytes;m[3]=bytes==2?1u:2u;m[4]=cropped;m[5]=source|0xa0000000u;return 0;
}
int rd_screen_capture(uint8_t *dest,uint32_t capacity,uint32_t m[6])
{return rd_screen_capture_area(dest,capacity,m,0);}
