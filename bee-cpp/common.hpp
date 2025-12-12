#pragma once
// ---- common helpers & config ----
#include <string>
#include <fstream>
#include <sstream>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <cctype>

// TCP helpers need these:
#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>

// ---------------- Config ----------------
struct Cfg {
    // BT
    int         bt_channel = 1;
    std::string bt_bind_mac;    // Bee local adapter MAC to bind (optional)
    std::string bt_allow_mac;   // Allowed remote MAC (Syra) (optional)

    // bands/grid
    int bands = 16;
    int bits  = 3;
    int cols  = 16;
    int rows  = 8;

    // display
    int i2c_bus  = 0;
    int i2c_addr = 60;
    int width    = 128;
    int height   = 64;

    // ports (preferred are the nested ones; we store in flat members)
    int port_udp_bands = 7001;   // UDP in (from Syra)
    int port_tcp_bands = 7003;   // TCP in (from BT bridge)
    int port_grid      = 7002;   // TCP to display driver

    int fps = 24;
};

// -------------- tiny JSON helpers (string-search; tolerant) ----------------

inline std::string slurp_file(const std::string& p) {
    std::ifstream f(p, std::ios::binary);
    std::ostringstream ss; ss << f.rdbuf();
    return ss.str();
}

// finds the first occurrence of "key": <number> anywhere in the JSON text
inline int json_get_int(const std::string& js, const std::string& key, int defv) {
    auto kpos = js.find("\""+key+"\"");
    if (kpos==std::string::npos) return defv;
    auto colon = js.find(':', kpos);
    if (colon==std::string::npos) return defv;
    auto p = js.find_first_of("-0123456789", colon+1);
    if (p==std::string::npos) return defv;
    int sign = (js[p]=='-')?-1:1; if (js[p]=='-') ++p;
    int v=0;
    while (p<js.size() && std::isdigit(static_cast<unsigned char>(js[p]))) {
        v = v*10 + (js[p]-'0'); ++p;
    }
    return v*sign;
}

// finds the first occurrence of "key": "value"
inline std::string json_get_str(const std::string& js, const std::string& key, const std::string& defv) {
    auto kpos = js.find("\""+key+"\"");
    if (kpos==std::string::npos) return defv;
    auto colon = js.find(':', kpos);
    if (colon==std::string::npos) return defv;
    auto q1 = js.find('"', colon+1);
    if (q1==std::string::npos) return defv;
    auto q2 = js.find('"', q1+1);
    if (q2==std::string::npos) return defv;
    return js.substr(q1+1, q2-(q1+1));
}

inline Cfg load_cfg(const std::string& path) {
    auto s = slurp_file(path);
    Cfg c;

    // ---- flat or nested — these finders match keys anywhere ----
    // BT + basic
    c.bt_channel   = json_get_int(s,"bt_channel",c.bt_channel);
    c.bt_bind_mac  = json_get_str(s,"bt_bind_mac",  c.bt_bind_mac);
    c.bt_allow_mac = json_get_str(s,"bt_allow_mac", c.bt_allow_mac);

    c.bands  = json_get_int(s,"bands",c.bands);
    c.bits   = json_get_int(s,"bits_per_band",c.bits);

    // grid.{cols,rows}
    c.cols   = json_get_int(s,"cols",c.cols);
    c.rows   = json_get_int(s,"rows",c.rows);

    // display.{i2c_bus,i2c_addr,width,height}
    c.i2c_bus  = json_get_int(s,"i2c_bus", c.i2c_bus);
    c.i2c_addr = json_get_int(s,"i2c_addr",c.i2c_addr);
    c.width    = json_get_int(s,"width",   c.width);
    c.height   = json_get_int(s,"height",  c.height);

    // Ports (prefer new nested names; keep legacy fallbacks)
    c.port_udp_bands = json_get_int(s,"udp_bands",
                           json_get_int(s,"bt_frames",c.port_udp_bands));
    c.port_tcp_bands = json_get_int(s,"tcp_bands",
                           json_get_int(s,"port_tcp_bands",c.port_tcp_bands));
    c.port_grid      = json_get_int(s,"grid_pixels",
                           json_get_int(s,"port_grid",c.port_grid));

    c.fps = json_get_int(s,"fps",c.fps);
    return c;
}

// -------------- 16×3-bit unpack (6 bytes → 16 values 0..7) -----------------
inline void unpack_3bit_16(const uint8_t in[6], uint8_t out[16]) {
    int bitpos=0;
    for (int i=0;i<16;++i) {
        int byte_idx = bitpos>>3;
        int off = bitpos & 7;
        uint16_t v = (in[byte_idx] >> off);
        int used = 8-off;
        if (used<3) v |= uint16_t(in[byte_idx+1]) << used;
        out[i] = uint8_t(v & 0x7);
        bitpos += 3;
    }
}

// ---------------------- TCP helpers (loopback only) -------------------------
inline int tcp_listen(uint16_t port) {
    int s = ::socket(AF_INET, SOCK_STREAM, 0);
    int yes=1; setsockopt(s,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof(yes));
    sockaddr_in a{}; a.sin_family=AF_INET;
    a.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    a.sin_port=htons(port);
    bind(s,(sockaddr*)&a,sizeof(a)); listen(s,1); return s;
}
inline int tcp_accept(int srv) { return ::accept(srv,nullptr,nullptr); }
inline int tcp_connect(uint16_t port) {
    int s = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in a{}; a.sin_family=AF_INET;
    a.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    a.sin_port=htons(port);
    connect(s,(sockaddr*)&a,sizeof(a)); return s;
}
inline bool recvn(int sock, void* buf, size_t n) {
    uint8_t* p=(uint8_t*)buf; size_t got=0;
    while (got<n) { ssize_t r=::recv(sock,p+got,n-got,0); if (r<=0) return false; got+=size_t(r); }
    return true;
}
inline bool sendn(int sock, const void* buf, size_t n) {
    const uint8_t* p=(const uint8_t*)buf; size_t sent=0;
    while (sent<n) { ssize_t r=::send(sock,p+sent,n-sent,0); if (r<=0) return false; sent+=size_t(r); }
    return true;
}
