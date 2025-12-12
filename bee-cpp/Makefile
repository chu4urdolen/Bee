CXX       = g++
CXXFLAGS  = -O2 -std=gnu++17 -Wall -Wextra -pthread
LDLIBS_BT = -lbluetooth
LDLIBS    = -pthread

.PHONY: all clean

all: bee_display_driver bee_spectrum bee_udp_server bee_bt_bridge

bee_display_driver: bee_display_driver.cpp common.hpp
	$(CXX) $(CXXFLAGS) -o $@ bee_display_driver.cpp $(LDLIBS)

bee_spectrum: bee_spectrum.cpp common.hpp
	$(CXX) $(CXXFLAGS) -o $@ bee_spectrum.cpp $(LDLIBS)

bee_udp_server: bee_udp_server.cpp common.hpp
	$(CXX) $(CXXFLAGS) -o $@ bee_udp_server.cpp $(LDLIBS)

bee_bt_bridge: bee_bt_bridge.cpp common.hpp
	$(CXX) $(CXXFLAGS) -o $@ bee_bt_bridge.cpp $(LDLIBS) $(LDLIBS_BT)

clean:
	rm -f bee_display_driver bee_spectrum bee_udp_server bee_bt_bridge
