// bee_spectrum_display.cpp
#include "common.hpp"
#include <vector>
#include <cstring>
#include <string>
#include <random>
#include <thread>
#include <chrono>
#include <algorithm>
#include <cstdio>   // fprintf
#include <cmath>

static void draw_bars(const uint8_t* bands, std::vector<uint8_t>& grid, int COLS, int ROWS){
    std::fill(grid.begin(), grid.end(), 0);
    for (int x=0; x<COLS && x<16; ++x){
        int h = std::min(ROWS, int(bands[x]));
        for (int r=0; r<h; ++r){
            int y = ROWS - 1 - r;
            grid[y*COLS + x] = 1;
        }
    }
}

int main(int argc, char** argv){
    Cfg cfg = load_cfg("bee_config.json");
    const int COLS = cfg.cols;
    const int ROWS = cfg.rows;

    // ---- debug flags (accept -debug / --debug and =noise|=bars) ----
    bool debug = false, debug_noise = false;
    for (int i=1;i<argc;++i){
        std::string a = argv[i];
        if (a.rfind("-debug", 0) == 0 || a.rfind("--debug", 0) == 0){
            debug = true;
            if (a.find("=noise") != std::string::npos) debug_noise = true;
        }
    }

    // connect to display driver (grid_pixels)
    int out = tcp_connect(static_cast<uint16_t>(cfg.port_grid));     // ports.grid_pixels
    if (out < 0) { std::fprintf(stderr, "connect port_grid failed\n"); return 1; }

    if (debug){
        // --- DEBUG LOOP: synthetic frames ---
        std::mt19937 rng{std::random_device{}()};
        std::uniform_int_distribution<int> band_dist(0, std::max(0, ROWS));
        std::uniform_int_distribution<int> px(0, std::max(0, COLS-1));
        std::uniform_int_distribution<int> py(0, std::max(0, ROWS-1));
        std::uniform_int_distribution<int> spark_cnt(0, COLS/2 + 1);

        uint8_t bands[16]{}; std::vector<uint8_t> grid(COLS*ROWS, 0);
        int frame=0, smooth_every=3;

        for(;;){
            if (!debug_noise){
                if (frame % smooth_every == 0){
                    for (int x=0;x<16;++x) bands[x] = uint8_t(std::min(ROWS, band_dist(rng)));
                } else {
                    for (int x=0;x<16;++x){
                        int v = int(bands[x]) + (rng()%3 - 1);
                        bands[x] = uint8_t(std::clamp(v, 0, ROWS));
                    }
                }
                draw_bars(bands, grid, COLS, ROWS);
            } else {
                std::fill(grid.begin(), grid.end(), 0);
                int n = spark_cnt(rng);
                for (int i=0;i<n;++i) grid[py(rng)*COLS + px(rng)] = 1;
            }

            if (!sendn(out, grid.data(), grid.size())) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(33));
            ++frame;
        }
        return 0;
    }

    // --- LIVE MODE: listen for 6-byte frames on tcp_bands ---
    int srv = tcp_listen(static_cast<uint16_t>(cfg.port_tcp_bands)); // ports.tcp_bands
    int bt  = tcp_accept(srv);
    uint8_t in6[6], bands[16];
    std::vector<uint8_t> grid(COLS*ROWS);

    while (recvn(bt, in6, 6)){
        unpack_3bit_16(in6, bands);
        draw_bars(bands, grid, COLS, ROWS);
        if (!sendn(out, grid.data(), grid.size())) break;
    }
    return 0;
}
