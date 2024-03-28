#define _POSIX_C_SOURCE 200112L

#include <unistd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <time.h>

#include <cairo.h>

#ifdef SDL_SIM

#include <SDL2/SDL.h>

#else

#include <linux/fb.h>
#include <sys/vt.h>

#endif

#define SHM_SIZE 32768

#define MAXWIDGETS 256
#define FLAG_ALIGN_RIGHT 1
#define FLAG_ALIGN_CENTER 2
#define FLAG_HIDE 2

#define VISFLAG_DUMP_PNG 0x80000000

#define MAXFONTS 16

#define IS_VISIBLE(cw, visibility) (cw->visgroup == (visibility & cw->vismask))
#define TILE_WIDGETS(x, y) (&tile_widgets[(y << 9) | (x << 5)])
#define TILE_NWIDGETS(x, y) tile_nwidgets[(y << 4) | x]

#define _TILEPOS(var, val, sz) var = (val) / sz; if (var < 0) var = 0; if (var > 15) var = 15

#define TILE_POS(tx1, tx2, ty1, ty2, x, y, w, h)        \
    _TILEPOS(tx1, x, tilesize_x);                       \
    _TILEPOS(ty1, y, tilesize_y);                       \
    _TILEPOS(tx2, (x) + (w), tilesize_x);               \
    _TILEPOS(ty2, (y) + (h), tilesize_y)

#define MAXICONS 32

typedef void(*blitfunc)(int x, int y, int w, int h);


typedef struct {
    int bpp, rl, gl, bl, ro, go, bo;
    cairo_format_t cairo_fmt;
    blitfunc blit;
} blitspec_t;

typedef struct {
    uint32_t version;
    uint32_t visgroup;
    uint32_t vismask;

    uint32_t cflags;
    double cxscale;
    uint32_t cfg;
    uint32_t cbg;
    uint32_t cstrike;
    int16_t cx;
    int16_t cy;
    uint16_t cw;
    uint16_t ch;
    int16_t cxo;
    int16_t cyo;
    uint8_t ctextsize;
    uint16_t ctextptr;
    uint8_t ctype;
    uint8_t cnchar;
    uint8_t cfont;
} widget_t;

typedef struct {
    uint32_t last_version;
    uint16_t dirty_mask;
    uint8_t ty1, ty2;
    int nchar;
    uint8_t visible;
    char* textbuf;
} widget_ldata_t;

typedef struct {
    uint32_t version;
    uint32_t numwidgets;
    uint32_t visibility;
} memheader_t;

typedef void(*drawfunc)(cairo_t* ctx, widget_t* cw, widget_ldata_t* ld);

static memheader_t cur_header;

static blitspec_t* blitspec;
static blitfunc blitter;

static cairo_t* frontctx;
static cairo_surface_t* frontsurf;
static cairo_surface_t* icons[MAXICONS];

static cairo_font_face_t* fonts[MAXFONTS];

static uint8_t* frontsurf_data;
static uint8_t* framebuffer;
static uint8_t* shmdata;
static volatile memheader_t* shmhdr;
static widget_t* widgets;

static int src_stride;
static int dst_stride;

static widget_ldata_t widget_ldata[MAXWIDGETS];

static uint16_t dirty_bits[16];
static uint8_t tile_widgets[256 * 32];
static uint8_t tile_nwidgets[256];

static int tilesize_x, tilesize_y;

static int screenw, screenh;

static time_t next_screen_refresh;

#ifdef SDL_SIM

static SDL_Window* sdlwindow;
static SDL_Renderer* renderer;
static SDL_Texture* texture;


#else
static struct fb_var_screeninfo vinfo;
static struct fb_fix_screeninfo finfo;

static int vt_fd, vt_number, vt_active, vt_active_requested;
#endif

static void blit_rgbx_rgb24(int x, int y, int w, int h) {
    uint8_t* srcbuf = &frontsurf_data[x * 4 + y * src_stride];
    uint8_t* dstbuf = &framebuffer[x * 3 + y * dst_stride];
    int line_inc_src = src_stride - (w * 4);
    int line_inc_dst = dst_stride - (w * 3);
    int xx, yy;
    for (yy = 0; yy < h; yy++) {
        for (xx = 0; xx < w; xx++) {
            uint32_t v = ((uint32_t*)srcbuf)[0];
            dstbuf[0] = v >> 16;
            dstbuf[1] = v >> 8;
            dstbuf[2] = v;
            srcbuf += 4;
            dstbuf += 3;
        }
        srcbuf += line_inc_src;
        dstbuf += line_inc_dst;
    }
}

void blit_rgbx_bgr24(int x, int y, int w, int h) {
    uint8_t* srcbuf = &frontsurf_data[x * 4 + y * src_stride];
    uint8_t* dstbuf = &framebuffer[x * 3 + y * dst_stride];
    int line_inc_src = src_stride - (w * 4);
    int line_inc_dst = dst_stride - (w * 3);
    int xx, yy;
    for (yy = 0; yy < h; yy++) {
        for (xx = 0; xx < w; xx++) {
            uint32_t v = ((uint32_t*)srcbuf)[0];
            dstbuf[0] = v;
            dstbuf[1] = v >> 8;
            dstbuf[2] = v >> 16;
            srcbuf += 4;
            dstbuf += 3;
        }
        srcbuf += line_inc_src;
        dstbuf += line_inc_dst;
    }
}

void blit_rgbx_rgbx(int x, int y, int w, int h) {
    uint8_t* srcbuf = &frontsurf_data[x * 4 + y * src_stride];
    uint8_t* dstbuf = &framebuffer[x * 4 + y * dst_stride];
    int yy;
    w *= 4;
    for (yy = 0; yy < h; yy++) {
        memcpy(dstbuf, srcbuf, w);
        srcbuf += src_stride;
        dstbuf += dst_stride;
    }
}

void blit_rgb565_rgb565(int x, int y, int w, int h) {
    uint8_t* srcbuf = &frontsurf_data[x * 2 + y * src_stride];
    uint8_t* dstbuf = &framebuffer[x * 2 + y * dst_stride];
    int yy;
    w *= 2;
    for (yy = 0; yy < h; yy++) {
        memcpy(dstbuf, srcbuf, w);
        srcbuf += src_stride;
        dstbuf += dst_stride;
    }
}

#ifdef SDL_SIM
void blit_rgbx_sdl(int x, int y, int w, int h) {
    uint8_t* srcbuf = &frontsurf_data[x * 4 + y * src_stride];
    void* pixels;
    uint8_t* dstbuf;
    int dst_stride;
    SDL_Rect r;
    int yy;

    r.x = x;
    r.y = y;
    r.w = w;
    r.h = h;

    if (SDL_LockTexture(texture, &r, &pixels, &dst_stride))
        return;

    w *= 4;

    dstbuf = (uint8_t*)pixels;
    for (yy = 0; yy < h; yy++) {
        memcpy(dstbuf, srcbuf, w);
        srcbuf += src_stride;
        dstbuf += dst_stride;
    }

    SDL_UnlockTexture(texture);
}
#endif

static blitspec_t blitters[] = {
#ifdef SDL_SIM
    { 24,  8, 8, 8,   0, 8, 16, CAIRO_FORMAT_RGB24, &blit_rgbx_sdl },
#else
    { 24,  8, 8, 8,   0, 8, 16, CAIRO_FORMAT_RGB24, &blit_rgbx_rgb24 },
    { 24,  8, 8, 8,  16, 8,  0, CAIRO_FORMAT_RGB24, &blit_rgbx_bgr24 },
    { 32,  8, 8, 8,  16, 8,  0, CAIRO_FORMAT_RGB24, &blit_rgbx_rgbx },
    { 16,  5, 6, 5,  11, 5,  0, CAIRO_FORMAT_RGB16_565, &blit_rgb565_rgb565 },
#endif
    { 0 }
};


void set_color_rgb(cairo_t* ctx, uint32_t c) {
    cairo_set_source_rgb(ctx, ((c >> 16) & 0xFF) / 255.0, ((c >> 8) & 0xFF) / 255.0, (c & 0xFF) / 255.0);
}

void set_color_rgba(cairo_t* ctx, uint32_t c) {
    cairo_set_source_rgba(ctx, ((c >> 16) & 0xFF) / 255.0, ((c >> 8) & 0xFF) / 255.0, (c & 0xFF) / 255.0, ((c >> 24) & 0xFF) / 255.0);
}

void clear_image(cairo_t* ctx, uint32_t color) {
    cairo_reset_clip(ctx);
    cairo_set_operator(ctx, CAIRO_OPERATOR_SOURCE);
    set_color_rgb(ctx, color);
    cairo_paint(ctx);
    cairo_set_operator(ctx, CAIRO_OPERATOR_OVER);
}

void mark_screen_dirty() {
    memset(dirty_bits, 0xFF, sizeof(dirty_bits));
}

void load_widgets() {
    int i;
    while (1) {
        uint32_t cvers = shmhdr->version;
        uint32_t nw = shmhdr->numwidgets;
        char* shmend = (char*)(shmdata + SHM_SIZE - 1);

        memset(tile_nwidgets, 0, sizeof(tile_nwidgets));

        if (nw > MAXWIDGETS)
            return;
        char* textbuf = (char*)(shmdata + (sizeof(memheader_t) + nw * sizeof(widget_t)));
        volatile widget_t* cw = widgets;
        widget_ldata_t* ld = widget_ldata;

        for (i = 0; i < nw; i++, cw++, ld++) {
            int tx1, tx2, ty1, ty2, xx, yy;
            TILE_POS(tx1, tx2, ty1, ty2, cw->cx, cw->cy, cw->cw, cw->ch);
            ld->dirty_mask = (2 << tx2) - (1 << tx1);
            ld->ty1 = ty1;
            ld->ty2 = ty2;
            for (yy = ty1; yy <= ty2; yy++) {
                for (xx = tx1; xx <= tx2; xx++) {
                    int tnw = TILE_NWIDGETS(xx, yy);
                    if (tnw < 32) {
                        TILE_WIDGETS(xx, yy)[tnw] = i;
                        TILE_NWIDGETS(xx, yy)++;
                    }
                }
            }
            ld->nchar = cw->cnchar;
            if (textbuf + ld->nchar > shmend) {
                ld->nchar = 0;
            } else {
                ld->textbuf = textbuf;
                textbuf += ld->nchar;
            }
            ld->last_version = cw->version - 1;
        }
        if (cvers == shmhdr->version) {
            cur_header.version = cvers;
            cur_header.numwidgets = nw;
            cur_header.visibility = 0;
            clear_image(frontctx, 0);
            mark_screen_dirty();
            break;
        }
    }
    for (i = 0; i < MAXICONS; i++) {
        char fname[32];
        cairo_surface_t* ico = icons[i];
        if (ico != NULL) {
            cairo_surface_destroy(ico);
        }
        sprintf(fname, "icon_%d.png", i);

    }
}

void draw_widget_text(cairo_t* ctx, widget_t* cw, widget_ldata_t* ld) {
    int fontidx = cw->cfont;
    if (fontidx >= MAXFONTS || !fonts[fontidx]) return;

    set_color_rgb(ctx, cw->cfg);

    cairo_set_font_face(ctx, fonts[fontidx]);
    cairo_set_font_size(ctx, cw->ctextsize);
    int xo = cw->cxo;
    int flags = cw->cflags;

    cairo_text_extents_t xt;
    if (flags & (FLAG_ALIGN_RIGHT|FLAG_ALIGN_CENTER)) {
        cairo_text_extents(ctx, ld->textbuf, &xt);
        if (flags & FLAG_ALIGN_CENTER) {
            xo += (cw->cw - xt.x_advance * cw->cxscale) / 2;
        } else {
            xo += cw->cw - xt.x_advance * cw->cxscale;
        }
    }
    cairo_translate(ctx, cw->cx + xo, cw->cy + cw->cyo);
    cairo_scale(ctx, cw->cxscale, 1);
    cairo_move_to(ctx, 0, 0);
    cairo_text_path(ctx, ld->textbuf);
    cairo_fill(ctx);
    cairo_identity_matrix(ctx);
    uint32_t strike = cw->cstrike;
    if (strike) {
        set_color_rgb(ctx, strike);
        cairo_move_to(ctx, cw->cx + .5, cw->cy + cw->ch / 2 + .5);
        cairo_set_line_width(ctx, 3);
        cairo_rel_line_to(ctx, cw->cw, 0);
        cairo_stroke(ctx);
    }
}

void draw_widget_icon(cairo_t* ctx, widget_t* cw, widget_ldata_t* ld) {

}

static drawfunc drawfuncs[] = { draw_widget_text };

void mark_dirty(int x, int y, int w, int h) {
    int tx1, tx2, ty1, ty2;
    TILE_POS(tx1, tx2, ty1, ty2, x, y, w, h);
    int ty;
    uint16_t mask;

    mask = (2 << tx2) - (1 << tx1);

    for (ty = ty1; ty <= ty2; ty++) {
        dirty_bits[ty] |= mask;
    }
}

void draw_widgets() {
    int i, j, k;
    widget_t* cw;
    widget_ldata_t* ld;
    uint8_t update_widgets[MAXWIDGETS];
    int numupdates;

#ifndef SDL_SIM
    if (vt_active != vt_active_requested) {
        vt_active = vt_active_requested;
        ioctl(vt_fd, VT_RELDISP, vt_active ? VT_ACKACQ : 1);
        if (vt_active) {
            clear_image(frontctx, 0);
            mark_screen_dirty();
        }
    }

    if (!vt_active)
        return;
#endif

    volatile memheader_t* hdr = (memheader_t*)shmdata;
    if (cur_header.version != hdr->version) {
        load_widgets();
    }

    numupdates = 0;

    uint32_t new_visibility = hdr->visibility;
    for (i = 0, cw = widgets, ld = widget_ldata; i < cur_header.numwidgets; i++, cw++, ld++) {
        uint32_t version = cw->version;
        int was_visible = IS_VISIBLE(cw, cur_header.visibility);
        int now_visible = IS_VISIBLE(cw, new_visibility);
        if ((now_visible && version != ld->last_version) || was_visible != now_visible) {
            int yy;
            //ld->updating_row = -1;
            ld->visible = now_visible;

            for (yy = ld->ty1; yy <= ld->ty2; yy++)
                dirty_bits[yy] |= ld->dirty_mask;
            ld->last_version = version;
        }
    }

    int cx, cy, ctx, cty, x1;

    for (cy = 0, cty = 0; cty < 16; cty++, cy += tilesize_y) {
        int ch = cty == 15 ? screenh - cy : tilesize_y;
        uint16_t mask = dirty_bits[cty];
        numupdates = 0;
        ctx = 0;
        cx = 0;
        x1 = 0;
        do {
            if (mask & 1) {
                uint8_t nw = TILE_NWIDGETS(ctx, cty);
                uint8_t* tw = TILE_WIDGETS(ctx, cty);
                for (i = 0; i < nw; i++) {
                    int wjtnum = tw[i];
                    ld = &widget_ldata[wjtnum];

                    if (ld->visible) {
                        // insertion sort into update_widgets
                        for (j = 0; j < numupdates; j++) {
                            if (wjtnum < update_widgets[j])
                                break;

                            //already in the list
                            if (wjtnum == update_widgets[j])
                                goto break_outer;
                        }
                        for (k = numupdates; k > j; k--) {
                            update_widgets[k] = update_widgets[k - 1];
                        }
                        numupdates++;
                        update_widgets[j] = wjtnum;
                    break_outer:;
                    }
                }
            }

            cx += tilesize_x;
            char z = mask & 3;
            if (z == 1) {
                if (cx > screenw) cx = screenw;
                cairo_reset_clip(frontctx);
                cairo_rectangle(frontctx, x1, cy, cx - x1, ch);
                cairo_clip(frontctx);
                set_color_rgb(frontctx, 0);
                cairo_paint(frontctx);

                for (i = 0; i < numupdates; i++) {
                    int wjtnum = update_widgets[i];
                    ld = &widget_ldata[wjtnum];
                    cw = &widgets[wjtnum];

                    if (i != 0) {
                        cairo_reset_clip(frontctx);
                        cairo_rectangle(frontctx, x1, cy, cx - x1, ch);
                        cairo_clip(frontctx);
                    }
                    cairo_rectangle(frontctx, cw->cx, cw->cy, cw->cw, cw->ch);
                    cairo_clip(frontctx);
                    if (cw->cbg) {
                        set_color_rgba(frontctx, cw->cbg);
                        cairo_paint(frontctx);
                    }
                    int type = cw->ctype;
                    if (type < (sizeof(drawfuncs)/sizeof(drawfunc))) {
                        drawfuncs[type](frontctx, cw, ld);
                    }
                }
                numupdates = 0;
            } else if (z == 2) {
                x1 = cx;
            }
            ctx++;
            mask >>= 1;
        } while (mask);
    }

    for (cy = 0, cty = 0; cty < 16; cty++, cy += tilesize_y) {
        int ch = cty == 15 ? screenh - cy : tilesize_y;
        uint16_t mask = dirty_bits[cty];
        cx = 0;
        x1 = 0;
        do {
            cx += tilesize_x;
            char z = mask & 3;
            if (z == 1) {
                if (cx > screenw) cx = screenw;
                blitter(x1, cy, cx - x1, ch);
            } else if (z == 2) {
                x1 = cx;
            }
            ctx++;
            mask >>= 1;
        } while (mask);
    }

    memset(dirty_bits, 0, sizeof(dirty_bits));
    cur_header.visibility = new_visibility;
}

#ifndef SDL_SIM

static void vt_release_sig(int signum) {
    vt_active_requested = 0;
}

static void vt_acq_sig(int signum) {
    vt_active_requested = 1;
}

static void init_vt(int vtno) {
    struct vt_mode vtm;
    struct termios tio;
    char vt_device[32];
    if (vtno <= 0) {
        int console_fd = open("/dev/console", O_RDWR);

        if (console_fd < 0) {
            perror("cannot open /dev/console");
            return;
        }

        if (ioctl(console_fd, VT_OPENQRY, &vtno) < 0) {
            close(console_fd);
            perror("VT_OPENQRY failed");
            return;
        }

        close(console_fd);
    }

    if (vtno < 0) {
        fprintf(stderr, "could not find open console\n");
        return;
    }

    sprintf(vt_device, "/dev/tty%d", vtno);
    vt_number = vtno;

    vt_fd = open(vt_device, O_RDWR);
    if (vt_fd < 0) {
        perror("cannot open tty");
        vt_fd = 0;
        return;
    }

    if (ioctl(vt_fd, VT_GETMODE, &vtm) < 0) {
        close(vt_fd);
        perror("VT_GETMODE failed");
        close(vt_fd);
        vt_fd = 0;
        return;
    }

    vtm.mode = VT_PROCESS;
    vtm.relsig = SIGUSR1;
    vtm.acqsig = SIGUSR2;

    if (ioctl(vt_fd, VT_SETMODE, &vtm) < 0) {
        perror("VT_SETMODE failed");
        close(vt_fd);
        vt_fd = 0;
        return;
    }

    signal(SIGUSR1, vt_release_sig);
    signal(SIGUSR2, vt_acq_sig);

    if (ioctl(vt_fd, VT_ACTIVATE, vtno) < 0) {
        perror("VT_ACTIVATE failed");
        close(vt_fd);
        vt_fd = 0;
        return;
    }

    if (tcgetattr(vt_fd, &tio) < 0) {
        perror("tcgetattr failed");
        close(vt_fd);
        vt_fd = 0;
        return;
    }

    tio.c_iflag = tio.c_oflag = 0;
    tio.c_lflag &= ~(ECHO|ISIG);

    if (tcsetattr(vt_fd, TCSANOW, &tio) < 0) {
        perror("tcsetattr failed");
        close(vt_fd);
        vt_fd = 0;
        return;
    }

    write(vt_fd, "\033[?25l", 6);
}

#endif

int main(int argc, char** argv) {

    const char* fbpath = "/dev/fb0";
    const char* shmpath = "/dev/shm/hud";
    int i, flip = 0, sim = 0, vtno = -1;

    for (i = 1; i < argc; i++) {
        char* arg = argv[i];
        if (*arg == '-') {
            arg++;
            while (*arg) {
                switch(*arg) {
                    case 'd':
                        if (++i == argc) {
                            fprintf(stderr, "-d: an argument is required\n");
                            return 1;
                        }
                        fbpath = argv[i];
                        break;
                    case 'f':
                        flip = 1;
                        break;
                    case 'v':
                        if (++i == argc) {
                            fprintf(stderr, "-v: an argument is required\n");
                            return 1;
                        }
                        vtno = atoi(argv[i]);
                        break;
                    case 'S':
                        sim = 1;
                        break;
                    case 's':
                        if (++i == argc) {
                            fprintf(stderr, "-s: an argument is required\n");
                            return 1;
                        }
                        shmpath = argv[i];
                        break;
                    default:
                        fprintf(stderr, "-%c: unrecognized option\n", *arg);
                        return 1;
                }
                arg++;
            }
        } else {
            fprintf(stderr, "%s: unrecognized option\n", arg);
            return 1;
        }
    }

#ifdef SDL_SIM
    screenw = 800;
    screenh = 480;

    if (SDL_Init(SDL_INIT_VIDEO) < 0) {
        SDL_LogError(SDL_LOG_CATEGORY_APPLICATION, "Couldn't initialize SDL: %s", SDL_GetError());
        return 1;
    }


    sdlwindow = SDL_CreateWindow("HUD",
                              SDL_WINDOWPOS_UNDEFINED,
                              SDL_WINDOWPOS_UNDEFINED,
                              screenw, screenh,
                              0);

    renderer = SDL_CreateRenderer(sdlwindow, -1, 0);

    texture = SDL_CreateTexture(renderer, SDL_PIXELFORMAT_ARGB8888, SDL_TEXTUREACCESS_STREAMING, screenw, screenh);

    blitspec = &blitters[0];

#else
    if (vtno != -1) {
        init_vt(vtno);
    }

    vt_active_requested = 1;
    vt_active = 1;

    int fbdev = open(fbpath, O_RDWR | (sim ? O_CREAT : 0), 0644);
    if (fbdev < 0) {
        perror("Could not open framebuffer");
        return 1;
    }
    if (sim) {
        screenw = 800;
        screenh = 480;
        vinfo.bits_per_pixel = 24;
        vinfo.red.length = 8;
        vinfo.green.length = 8;
        vinfo.blue.length = 8;
        vinfo.red.offset = 0;
        vinfo.green.offset = 8;
        vinfo.blue.offset = 16;

        finfo.line_length = screenw * 3;
        finfo.smem_len = finfo.line_length * screenh;
        ftruncate(fbdev, finfo.smem_len);

    } else {
        if (ioctl(fbdev, FBIOGET_FSCREENINFO, &finfo)) {
            perror("Could not get fixed framebuffer info");
            return 1;
        }

        if (ioctl(fbdev, FBIOGET_VSCREENINFO, &vinfo)) {
            perror("Could not get variable framebuffer info");
            return 1;
        }
        screenw = vinfo.xres;
        screenh = vinfo.yres;
    }

    dst_stride = finfo.line_length;

    for (blitspec = blitters; ; blitspec++) {
        if (!blitspec->bpp) {
            fprintf(stderr, "Could not find blitspec!\n");
            return 1;
        }
        if (blitspec->bpp == vinfo.bits_per_pixel
            && blitspec->rl == vinfo.red.length
            && blitspec->gl == vinfo.green.length
            && blitspec->bl == vinfo.blue.length
            && blitspec->ro == vinfo.red.offset
            && blitspec->go == vinfo.green.offset
            && blitspec->bo == vinfo.blue.offset) {
            break;
        }
    }

    framebuffer = (uint8_t*)mmap(0, finfo.smem_len, PROT_READ | PROT_WRITE, MAP_SHARED, fbdev, 0);
    if (framebuffer == MAP_FAILED) {
        perror("Could not map framebuffer");
    }
    close(fbdev);

#endif

    tilesize_x = (screenw + 15) / 16;
    tilesize_y = (screenh + 15) / 16;

    int shmfd = open(shmpath, O_RDWR | O_CREAT, 0644);
    if (shmfd < 0) {
        perror("Could not open or create shared memory");
        return 1;
    }
    off_t pos = lseek(shmfd, 0, SEEK_END);
    if (pos < SHM_SIZE) {
        ftruncate(shmfd, SHM_SIZE);
    }
    shmdata = (uint8_t*)mmap(0, SHM_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, shmfd, 0);
    if (shmdata == MAP_FAILED) {
        perror("Could not map shared memory");
    }

    memheader_t* hdr = (memheader_t*)shmdata;
    shmhdr = hdr;
    widgets = (widget_t*)(hdr + 1);
    close(shmfd);

    fonts[0] = cairo_toy_font_face_create("sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
    fonts[1] = cairo_toy_font_face_create("monospace", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);

    blitter = blitspec->blit;
    frontsurf = cairo_image_surface_create(blitspec->cairo_fmt, screenw, screenh);
    frontsurf_data = cairo_image_surface_get_data(frontsurf);
    src_stride = cairo_image_surface_get_stride(frontsurf);

    frontctx = cairo_create(frontsurf);

    if (flip) {
        frontsurf_data += src_stride * (screenh - 1);
        src_stride = -src_stride;
    }

    load_widgets();
    for(;;) {
        struct timespec time_start;
        clock_gettime(CLOCK_MONOTONIC, &time_start);

        /* Kernel messages might stomp on the screen. Periodically refresh the whole thing
           just in case. */
        if (time_start.tv_sec >= next_screen_refresh) {
            mark_screen_dirty();
            next_screen_refresh = time_start.tv_sec + 20;
        }

        draw_widgets();

#ifdef SDL_SIM
        SDL_RenderCopy(renderer, texture, NULL, NULL);
        SDL_RenderPresent(renderer);

        SDL_Event event;
        while (SDL_PollEvent(&event)) {
            switch (event.type) {
                case SDL_QUIT:
                    SDL_Quit();
                    return 0;
                case SDL_KEYDOWN:
                    switch(event.key.keysym.sym) {
                        case SDLK_ESCAPE:
                            SDL_Quit();
                            return 0;
                        case SDLK_1: printf("muw\n"); fflush(stdout); break;
                        case SDLK_2: printf("muk\n"); fflush(stdout); break;
                        case SDLK_3: printf("muc\n"); fflush(stdout); break;
                        case SDLK_4: printf("mub\n"); fflush(stdout); break;
                        case SDLK_5: printf("mug\n"); fflush(stdout); break;
                        case SDLK_6: printf("mur\n"); fflush(stdout); break;
                        case SDLK_q: printf("muwl\n"); fflush(stdout); break;
                        case SDLK_w: printf("mukl\n"); fflush(stdout); break;
                        case SDLK_e: printf("mucl\n"); fflush(stdout); break;
                        case SDLK_r: printf("mubl\n"); fflush(stdout); break;
                        case SDLK_t: printf("mugl\n"); fflush(stdout); break;
                        case SDLK_y: printf("murl\n"); fflush(stdout); break;
                        case SDLK_RIGHT: printf("muu\n"); fflush(stdout); break;
                        case SDLK_LEFT: printf("mud\n"); fflush(stdout); break;
                        case SDLK_DOWN: printf("muc\n"); fflush(stdout); break;

                    }
                    break;
            }
        }
#endif
        if (cur_header.visibility & VISFLAG_DUMP_PNG) {
            char filename[32];
            struct timespec realtime;
            clock_gettime(CLOCK_REALTIME, &realtime);
            uint64_t ctms = (uint64_t)realtime.tv_sec * 1000;
            ctms += realtime.tv_nsec / 1000000;
            snprintf(filename, 32, "hudcap/cap-%10llu.png", ctms);
            cairo_surface_write_to_png(frontsurf, filename);
        }

        time_start.tv_nsec += 50000000;
        if (time_start.tv_nsec >= 1000000000) {
            time_start.tv_nsec -= 1000000000;
            time_start.tv_sec += 1;
        }

        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &time_start, NULL) != 0);
    }
    return 0;
}
