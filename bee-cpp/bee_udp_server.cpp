// bee_udp_bridge.cpp
#include "common.hpp"
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <random>
#include <thread>
#include <chrono>
#include <algorithm>

// pack 16 values (0..7) into 6 bytes (3 bits each)
static void pack_3bit_16(const uint8_t in[16], uint8_t out[6]){
    std::memset(out, 0, 6);
    int bitpos = 0;
    for (int i=0;i<16;++i){
        uint32_t v = (in[i] & 0x7);
        int b = bitpos >> 3;
        int o = bitpos & 7;
        out[b] |= (uint8_t)((v << o) & 0xFF);
        if (o > 5) out[b+1] |= (uint8_t)(v >> (8 - o));
        bitpos += 3;
    }
}

static bool tcp_reconnect(int& s, uint16_t port){
    if (s >= 0) ::close(s);
    s = tcp_connect(port);
    return s >= 0;
}

int main(int argc, char** argv){
    // --- args: -debug or -debug=noise | -debug=bars ---
    bool debug = false, debug_noise = false;
    for (int i=1;i<argc;++i){
        std::string a = argv[i];
        if (a.rfind("-debug",0)==0 || a.rfind("--debug",0)==0){
            debug = true;
            if (a.find("=noise") != std::string::npos) debug_noise = true;
        }
    }

    Cfg cfg = load_cfg("bee_config.json");

    // connect to spectrum TCP first (so debug can start immediately)
    int up = tcp_connect(static_cast<uint16_t>(cfg.port_tcp_bands));
    if (up < 0) std::fprintf(stderr,"tcp_connect(%d) failed, will retry on demand\n", cfg.port_tcp_bands);

    if (debug){
        // ---- DEBUG MODE: synthesize bands → pack → send to spectrum TCP ----
        std::mt19937 rng{std::random_device{}()};
        std::uniform_int_distribution<int> band_dist(0, std::max(0, cfg.rows)); // visual scale help
        std::uniform_int_distribution<int> jitter(-1,1);

        uint8_t bands16[16]{};
        uint8_t frame6[6]{};

        // init
        for (int x=0;x<16;++x) bands16[x] = uint8_t(std::min(cfg.rows, band_dist(rng)));

        double phase = 0.0;
        const int fps = std::max(1, cfg.fps);

        for(;;){
            if (!debug_noise){
                // soft sine bars with jitter
                for (int x=0;x<16;++x){
                    double s = (std::sin(phase + x*0.35) + 1.0) * 0.5; // 0..1
                    int v = int(std::round(s * 7.0)) + jitter(rng);
                    bands16[x] = (uint8_t)std::clamp(v, 0, 7);
                }
                phase += 0.12;
            } else {
                // sparkles
                for (int x=0;x<16;++x) bands16[x] = (rng() & 1) ? (uint8_t)(rng()%8) : 0;
            }

            pack_3bit_16(bands16, frame6);

            if (up < 0 && !tcp_reconnect(up, (uint16_t)cfg.port_tcp_bands)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(250));
                continue;
            }
            if (!sendn(up, frame6, 6)) {
                // try to reconnect next tick
                ::close(up); up = -1;
            }

            std::this_thread::sleep_for(std::chrono::milliseconds(1000 / fps));
        }
        return 0;
    }

    // ---- NORMAL MODE: UDP in → TCP out ----
    int us = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (us < 0) { std::perror("socket(udp)"); return 1; }

    sockaddr_in addr{}; addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)cfg.port_udp_bands);
    if (bind(us, (sockaddr*)&addr, sizeof(addr)) < 0) {
        std::perror("bind(udp)"); return 1;
    }

    uint8_t buf[6];
    for (;;) {
        ssize_t r = recv(us, buf, sizeof(buf), 0);
        if (r != 6) continue;

        if (up < 0 && !tcp_reconnect(up, (uint16_t)cfg.port_tcp_bands)) {
            // drop until we reconnect
            continue;
        }
        if (!sendn(up, buf, 6)) {
            ::close(up); up = -1;
        }
    }
}
