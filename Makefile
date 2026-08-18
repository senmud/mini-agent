CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra

# 优先用 curl-config 探测 libcurl，探测不到则退回 -lcurl
CURL_LIBS := $(shell curl-config --libs 2>/dev/null)
ifeq ($(strip $(CURL_LIBS)),)
CURL_LIBS := -lcurl
endif

all: mini-agent

mini-agent: agent.c
	$(CC) $(CFLAGS) -o $@ agent.c $(CURL_LIBS)

# 离线单元测试（不发网络请求）
test: test_agent.c agent.c
	$(CC) -O2 -Wall -Wno-unused-function -o test_agent test_agent.c $(CURL_LIBS)
	./test_agent

# 本地 mock 服务联调测试（无需外网，需已安装 python3）
# 注：Python 冷启动需要几百毫秒，必须等端口就绪后再跑 mini-agent
WAIT_READY = for probe in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25; do \
	curl -s -m 1 -o /dev/null --noproxy '*' http://127.0.0.1:18923/ && break; \
	sleep 0.2; done

mock-test: mini-agent
	@echo '== [1/2] SSE 流式测试 =='
	python3 tests/mock_server.py 18923 & \
	$(WAIT_READY); \
	NO_PROXY=127.0.0.1 MOCK_KEY=test-key ./mini-agent -c tests/mock_config.ini -s tests/system_mock.txt -r tests/requests_mock.txt; \
	wait
	@echo; echo '== [2/2] 非流式测试 =='
	python3 tests/mock_server.py 18923 & \
	$(WAIT_READY); \
	NO_PROXY=127.0.0.1 MOCK_KEY=test-key ./mini-agent -c tests/mock_config.ini -m mock-block -r tests/requests_one.txt; \
	wait

clean:
	rm -f mini-agent test_agent

.PHONY: all test mock-test clean
