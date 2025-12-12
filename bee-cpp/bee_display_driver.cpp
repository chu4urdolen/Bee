// bee_display_driver.cpp — fast OLED driver w/ dirty-page skipping
#include "common.hpp"
#include <fcntl.h>
#include <sys/ioctl.h>
#include <linux/i2c-dev.h>
#include <unistd.h>
#include <cstring>
#include <vector>
#include <thread>
#include <chrono>
#include <random>
#include <cmath>
#include <cstdio>
#include <string>
#include <algorithm>

// Minimal SSD1306 over I2C (monochrome, PAGE mode)
struct SSD1306 {
    int fd=-1, w=128, h=64;
    uint8_t addr=0x3C;
    // cache for dirty-page skipping
    std::vector<uint8_t> prev; // size = w * (h/8)
    bool first_draw = true;

    bool begin(int bus, int addr7, int W, int H) {
        w=W; h=H; addr=uint8_t(addr7);
        std::string dev = "/dev/i2c-" + std::to_string(bus);
        fd = ::open(dev.c_str(), O_RDWR);
        if (fd<0) return false;
        if (ioctl(fd, I2C_SLAVE, addr7) < 0) return false;
        init();
        prev.assign(w*(h/8), 0);    // prime cache
        first_draw = true;          // force first full refresh
        return true;
    }

    inline void cmd(uint8_t c){
        uint8_t b[2]={0x00,c};
        (void)::write(fd,b,2);
    }

    // Chunked DATA writes to avoid oversized I2C bursts
    inline void data(const uint8_t* d, size_t n){
        constexpr size_t CH = 64; // bumped from 16 -> 64 (usually safe; drop to 32 if needed)
        uint8_t buf[1+CH];
        buf[0]=0x40;
        for(size_t i=0;i<n;i+=CH){
            size_t k = std::min(CH, n - i);
            std::memcpy(&buf[1], d+i, k);
            (void)::write(fd, buf, 1+k);
        }
    }

    void init(){
        // Known-good power-up for SSD1306 128x64, internal VCC, PAGE addressing
        cmd(0xAE);                 // display OFF
        cmd(0xD5); cmd(0x80);      // clock divide
        cmd(0xA8); cmd(h-1);       // multiplex ratio
        cmd(0xD3); cmd(0x00);      // display offset
        cmd(0x40);                 // start line = 0
        cmd(0x8D); cmd(0x14);      // CHARGE PUMP ON (internal VCC)
        cmd(0x20); cmd(0x02);      // MEMORY MODE = PAGE ADDRESSING
        cmd(0xA1);                 // segment remap (mirror horizontally)
        cmd(0xC8);                 // COM scan direction (remap)
        cmd(0xDA); cmd((h==64)?0x12:0x02); // COM pins
        cmd(0x81); cmd(0x7F);      // contrast
        cmd(0xD9); cmd(0xF1);      // precharge
        cmd(0xDB); cmd(0x40);      // VCOM detect
        cmd(0xA4);                 // resume to RAM content
        cmd(0xA6);                 // normal (not inverted)
        cmd(0xAF);                 // display ON
    }

    // Push full buffer (PAGE-addressed) — but skip unchanged pages
    void draw(const std::vector<uint8_t>& pages) {
        const int pages_n = h/8;
        for (int p=0;p<pages_n;++p){
            const uint8_t* src = &pages[p*w];
            uint8_t*       dst = prev.empty() ? nullptr : &prev[p*w];

            bool changed = first_draw;
            if (!changed && dst) changed = std::memcmp(src, dst, w) != 0;
            if (!changed) continue;

            // set page & column window
            cmd(0xB0 + p);   // page
            cmd(0x00);       // low col = 0
            cmd(0x10);       // high col = 0
            data(src, w);

            if (dst) std::memcpy(dst, src, w);
        }
        first_draw = false;
    }
};

static void blit_grid_to_fb(const std::vector<uint8_t>& grid, int COLS, int ROWS,
                            int scrW, int scrH, std::vector<uint8_t>& fb)
{
    auto fill_rect = [&](int x0,int y0,int x1,int y1){
        if (x0<0) x0=0;
        if (y0<0) y0=0;
        if (x1>=scrW) x1=scrW-1;
        if (y1>=scrH) y1=scrH-1;
        for (int y=y0;y<=y1;++y){
            int page = y>>3, bit = y&7;
            int base = page*scrW;
            for (int x=x0;x<=x1;++x){
                fb[base + x] |= (1u<<bit);
            }
        }
    };

    std::fill(fb.begin(), fb.end(), 0);
    int idx=0;
    for (int gy=0; gy<ROWS; ++gy){
        for (int gx=0; gx<COLS; ++gx){
            if (grid[idx++]) {
                int x0 = gx*(scrW/COLS);
                int y0 = gy*(scrH/ROWS);
                int x1 = x0 + (scrW/COLS) - 1;
                int y1 = y0 + (scrH/ROWS) - 1;
                fill_rect(x0,y0,x1,y1);
            }
        }
    }
}

int main(int argc, char** argv){
    bool debug = false;
    enum class Demo { Bars, Noise } demo = Demo::Bars;

    for (int i=1;i<argc;++i){
        std::string a = argv[i];
        if (a == "-debug") { debug = true; }
        else if (a == "-debug=bars")  { debug = true; demo = Demo::Bars; }
        else if (a == "-debug=noise") { debug = true; demo = Demo::Noise; }
    }

    Cfg cfg = load_cfg("bee_config.json");
    const int COLS=cfg.cols, ROWS=cfg.rows;
    const int PAGES = cfg.height/8;
    const int FPS = std::max(1, cfg.fps);

    SSD1306 oled;
    if (!oled.begin(cfg.i2c_bus, cfg.i2c_addr, cfg.width, cfg.height)) {
        std::perror("oled.begin");
        return 1;
    }

    std::vector<uint8_t> fb(cfg.width * PAGES, 0);

    if (debug) {
        std::mt19937 rng{std::random_device{}()};
        std::uniform_int_distribution<int> bit01(0,1);
        double t = 0.0;

        while (true) {
            std::vector<uint8_t> grid(COLS*ROWS, 0);

            if (demo == Demo::Noise) {
                for (int i=0;i<COLS*ROWS;++i) grid[i] = (uint8_t)bit01(rng);
            } else {
                for (int x=0; x<COLS; ++x) {
                    double phase = t + x*0.35;
                    int h = (int)std::round(((std::sin(phase)+1.0)*0.5)*(ROWS));
                    h = std::max(0, std::min(ROWS, h));
                    for (int r=0; r<h; ++r) {
                        int y = ROWS - 1 - r;
                        grid[y*COLS + x] = 1;
                    }
                }
                t += 0.12; // speed
            }

            blit_grid_to_fb(grid, COLS, ROWS, cfg.width, cfg.height, fb);
            oled.draw(fb); // now skips unchanged pages
            std::this_thread::sleep_for(std::chrono::milliseconds(1000 / FPS));
        }
    }

    // --- NORMAL MODE: listen for grid frames over TCP and draw ---
    int srv = tcp_listen(static_cast<uint16_t>(cfg.port_grid));    // ports.grid_pixels
    int cli = tcp_accept(srv);

    std::vector<uint8_t> grid(COLS*ROWS);
    while (recvn(cli, grid.data(), grid.size())) {
        blit_grid_to_fb(grid, COLS, ROWS, cfg.width, cfg.height, fb);
        oled.draw(fb); // dirty-page skipping here too
    }
    (void)::close(cli); (void)::close(srv);
    return 0;
}
