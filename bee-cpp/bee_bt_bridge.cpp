// bee_bt_bridge.cpp — RFCOMM 6B-in → TCP out, with local adapter bind + MAC allowlist + SDP + piscan
#include "common.hpp"
#include <bluetooth/bluetooth.h>
#include <bluetooth/rfcomm.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <chrono>
#include <random>
#include <cmath>
#include <algorithm>

// --- Small helpers ---------------------------------------------------------

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

static std::string ba_to_str(const bdaddr_t& a){
    char buf[19] = {0};
    ba2str(&a, buf);
    return std::string(buf);
}

// --- Bring up adapter + enable page/inquiry scan (connectable) --------------

static void ensure_adapter_up_and_scannable(){
    const char* dev = std::getenv("BEE_HCI");
    std::string hci = dev && *dev ? dev : "hci0";

    // Be noisy once so you can see this happened
    std::fprintf(stderr, "[bt] ensuring %s is up + piscan\n", hci.c_str());

    // Power up + allow paging (connect) + inquiry (discover). Ignore failures.
    char cmd[256];

    std::snprintf(cmd, sizeof(cmd), "hciconfig %s up >/dev/null 2>&1", hci.c_str());
    (void)std::system(cmd);

    std::snprintf(cmd, sizeof(cmd), "hciconfig %s piscan >/dev/null 2>&1", hci.c_str());
    (void)std::system(cmd);

    // Also try btmgmt (some distros prefer mgmt API); ignore if absent.
    std::snprintf(cmd, sizeof(cmd), "btmgmt -i %s power on >/dev/null 2>&1", hci.c_str());
    (void)std::system(cmd);
    std::snprintf(cmd, sizeof(cmd), "btmgmt -i %s connectable on >/dev/null 2>&1", hci.c_str());
    (void)std::system(cmd);
    std::snprintf(cmd, sizeof(cmd), "btmgmt -i %s bondable off >/dev/null 2>&1", hci.c_str());
    (void)std::system(cmd);
}

// --- RFCOMM listen/accept with optional local bind & remote allow -----------

struct BtListenCfg {
    uint8_t  channel = 3;       // RFCOMM channel
    bool     have_bind = false; // bind to specific local adapter
    bdaddr_t bind_addr{};       // local adapter
    bool     have_allow = false;// restrict remote peer
    bdaddr_t allow_addr{};      // allowed peer (e.g., DIVA-SCRIPTS on Syra)
};

static int rfcomm_accept_one(const BtListenCfg& bcfg, bdaddr_t* out_remote){
    int srv = ::socket(AF_BLUETOOTH, SOCK_STREAM, BTPROTO_RFCOMM);
    if (srv < 0){ std::perror("socket(rfcomm)"); return -1; }

    int one = 1;
    (void)::setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    sockaddr_rc loc{}; loc.rc_family = AF_BLUETOOTH;
    if (bcfg.have_bind) {
        loc.rc_bdaddr = bcfg.bind_addr;   // bind to specific local controller
    } else {
        bdaddr_t any{}; std::memset(&any, 0, sizeof(any));
        loc.rc_bdaddr = any;              // BDADDR_ANY
    }
    loc.rc_channel = bcfg.channel;

    if (bind(srv, (sockaddr*)&loc, sizeof(loc)) < 0){
        std::perror("bind(rfcomm)"); ::close(srv); return -1;
    }
    if (listen(srv, 1) < 0){
        std::perror("listen(rfcomm)"); ::close(srv); return -1;
    }

    sockaddr_rc rem{}; socklen_t rlen = sizeof(rem);
    int cli = accept(srv, (sockaddr*)&rem, &rlen);
    ::close(srv);
    if (cli < 0){ std::perror("accept(rfcomm)"); return -1; }

    if (out_remote) *out_remote = rem.rc_bdaddr;

    if (bcfg.have_allow) {
        if (std::memcmp(&rem.rc_bdaddr, &bcfg.allow_addr, sizeof(bdaddr_t)) != 0) {
            std::fprintf(stderr, "[bt] reject remote %s (not allowed)\n",
                         ba_to_str(rem.rc_bdaddr).c_str());
            ::close(cli);
            return -2; // rejected
        }
    }
    return cli;
}

// --- Optional SDP registration (simple, via sdptool) ------------------------

static void ensure_sdp_sp(uint8_t channel){
    char cmd[128];
    std::snprintf(cmd, sizeof(cmd), "sdptool add --channel %u SP >/dev/null 2>&1", (unsigned)channel);
    (void)std::system(cmd);
}

// --- Main -------------------------------------------------------------------

int main(int argc, char** argv){
    bool debug=false, debug_noise=false;
    for (int i=1;i<argc;++i){
        std::string a = argv[i];
        if (a.rfind("-debug",0)==0 || a.rfind("--debug",0)==0){
            debug = true;
            if (a.find("=noise") != std::string::npos) debug_noise = true;
        }
    }

    Cfg cfg = load_cfg("bee_config.json");

    // Build BT listen cfg from JSON or env
    BtListenCfg bcfg;
    bcfg.channel = static_cast<uint8_t>(cfg.bt_channel);

    const char* bind_str  = cfg.bt_bind_mac.empty()  ? std::getenv("BEE_BT_BIND")  : cfg.bt_bind_mac.c_str();
    const char* allow_str = cfg.bt_allow_mac.empty() ? std::getenv("BEE_BT_ALLOW") : cfg.bt_allow_mac.c_str();

    if (bind_str && *bind_str){
        if (str2ba(bind_str, &bcfg.bind_addr) == 0){
            bcfg.have_bind = true;
            std::fprintf(stderr, "[bt] binding local adapter: %s\n", bind_str);
        } else {
            std::fprintf(stderr, "[bt] WARNING: invalid bt_bind_mac '%s'\n", bind_str);
        }
    }
    if (allow_str && *allow_str){
        if (str2ba(allow_str, &bcfg.allow_addr) == 0){
            bcfg.have_allow = true;
            std::fprintf(stderr, "[bt] allowing only remote: %s\n", allow_str);
        } else {
            std::fprintf(stderr, "[bt] WARNING: invalid bt_allow_mac '%s'\n", allow_str);
        }
    }

    // Upstream TCP: use cfg.port_tcp_bands (already mapped from ports.tcp_bands)
    const uint16_t tcp_port = static_cast<uint16_t>(cfg.port_tcp_bands);
    int up = tcp_connect(tcp_port);
    if (up < 0) std::fprintf(stderr,"tcp_connect(%u) failed, will retry on demand\n", tcp_port);

    // Make adapter discoverable + connectable, and ensure SP record exists.
    ensure_adapter_up_and_scannable();
    ensure_sdp_sp(bcfg.channel);

    if (debug){
        std::mt19937 rng{std::random_device{}()};
        uint8_t bands16[16]{};
        uint8_t frame6[6]{};
        double phase = 0.0;
        const int fps = std::max(1, cfg.fps);

        for(;;){
            if (!debug_noise){
                for (int x=0;x<16;++x){
                    double s = (std::sin(phase + x*0.35) + 1.0) * 0.5;
                    int v = int(std::round(s * 7.0));
                    bands16[x] = (uint8_t)std::clamp(v, 0, 7);
                }
                phase += 0.12;
            } else {
                for (int x=0;x<16;++x) bands16[x] = (rng() & 1) ? (uint8_t)(rng()%8) : 0;
            }

            pack_3bit_16(bands16, frame6);

            if (up < 0 && !tcp_reconnect(up, tcp_port)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(250));
                continue;
            }
            if (!sendn(up, frame6, 6)) { ::close(up); up=-1; }

            std::this_thread::sleep_for(std::chrono::milliseconds(1000 / fps));
        }
    }

    for(;;){
        std::string s_local  = bcfg.have_bind  ? (", local="+ba_to_str(bcfg.bind_addr)) : "";
        std::string s_allow  = bcfg.have_allow ? (", allow="+ba_to_str(bcfg.allow_addr)) : "";
        std::fprintf(stderr,"[bt] waiting on RFCOMM ch=%u%s%s…\n",
                     (unsigned)bcfg.channel, s_local.c_str(), s_allow.c_str());

        bdaddr_t remote{};
        int cli = rfcomm_accept_one(bcfg, &remote);
        if (cli == -2) continue;      // rejected
        if (cli < 0){ std::this_thread::sleep_for(std::chrono::milliseconds(500)); continue; }

        std::fprintf(stderr, "[bt] connected <- %s\n", ba_to_str(remote).c_str());

        if (up < 0 && !tcp_reconnect(up, tcp_port)) {
            std::fprintf(stderr,"[bt] upstream not ready; closing client.\n");
            ::close(cli);
            continue;
        }

        uint8_t f[6];
        while (recvn(cli, f, sizeof(f))){
            if (up < 0 && !tcp_reconnect(up, tcp_port)) break;
            if (!sendn(up, f, sizeof(f))) { ::close(up); up=-1; break; }
        }
        ::close(cli);
        std::fprintf(stderr, "[bt] client disconnected\n");
    }
}
