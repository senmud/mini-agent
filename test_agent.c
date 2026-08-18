/*
 * mini-agent 离线单元测试：验证请求切分、JSON 解析/转义、上下文裁剪、请求体构造。
 * 不发起任何网络请求。运行: make test
 */
#define AGENT_NO_MAIN
#include "agent.c"

#include <fcntl.h> /* open(O_WRONLY)，配合 dup2 在测试中屏蔽输出 */

static int failures = 0;

#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            fprintf(stderr, "FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
            failures++;                                                   \
        }                                                                 \
    } while (0)

static void test_split_requests(void)
{
    /* 3 个换行分隔请求；2 个换行属于请求内部；4+ 个换行也视为分隔 */
    char text[] = "第一个请求\n\n\n第二个请求\n\n仍然属于第二个请求\n\n\n\n第三个请求\n";
    normalize_newlines(text);
    size_t n = 0;
    char **reqs = split_requests(text, &n);
    CHECK(n == 3);
    if (n == 3) {
        CHECK(strcmp(reqs[0], "第一个请求") == 0);
        CHECK(strcmp(reqs[1], "第二个请求\n\n仍然属于第二个请求") == 0);
        CHECK(strcmp(reqs[2], "第三个请求") == 0);
    }
    for (size_t i = 0; i < n; i++)
        free(reqs[i]);
    free(reqs);

    /* CRLF 归一化后同样可切分 */
    char crlf[] = "req A\r\n\r\n\r\nreq B\r\n";
    normalize_newlines(crlf);
    char **r2 = split_requests(crlf, &n);
    CHECK(n == 2);
    if (n == 2) {
        CHECK(strcmp(r2[0], "req A") == 0);
        CHECK(strcmp(r2[1], "req B") == 0);
    }
    for (size_t i = 0; i < n; i++)
        free(r2[i]);
    free(r2);
}

static void test_json_parse(void)
{
    const char *obj =
        "{\"choices\":[{\"delta\":{\"content\":\"\\u4f60\\u597d world\","
        "\"reasoning_content\":\"a\\\"b\"},\"finish_reason\":null}],\"id\":\"x\"}";

    char *c = first_choice_field(obj, "delta", "content");
    CHECK(c && strcmp(c, "你好 world") == 0);
    free(c);

    char *r = first_choice_field(obj, "delta", "reasoning_content");
    CHECK(r && strcmp(r, "a\"b") == 0);
    free(r);

    /* 字段为 null 时返回 NULL */
    CHECK(first_choice_field(obj, "delta", "finish_reason") == NULL);
    /* 不存在的字段 */
    CHECK(first_choice_field(obj, "delta", "no_such") == NULL);

    /* 错误体解析 */
    const char *err = "{\"error\":{\"message\":\"Invalid API key\",\"code\":\"invalid_api_key\"}}";
    char *em = extract_error_message(err);
    CHECK(em && strcmp(em, "Invalid API key") == 0);
    free(em);

    /* 代理对 \uD83D\uDE00 => U+1F600 (😀 = f0 9f 98 80) */
    const char *surr = "{\"c\":\"\\ud83d\\ude00!\"}";
    char *s = js_get_string(surr, "c");
    CHECK(s && strcmp(s, "\xf0\x9f\x98\x80!") == 0);
    free(s);

    /* 嵌套对象不应干扰顶层 key 查找 */
    const char *nested = "{\"a\":{\"content\":\"inner\"},\"content\":\"outer\"}";
    char *o = js_get_string(nested, "content");
    CHECK(o && strcmp(o, "outer") == 0);
    free(o);
}

static void test_escape_roundtrip(void)
{
    dbuf b;
    dbuf_init(&b);
    const char *raw = "line1\nline2 \"quoted\" \\ \t结束 \x01";
    dbuf_str(&b, "{\"v\":\"");
    dbuf_json_escape(&b, raw);
    dbuf_str(&b, "\"}");
    char *v = js_get_string(b.data, "v");
    CHECK(v && strcmp(v, raw) == 0);
    free(v);
    dbuf_free(&b);
}

static void test_build_body(void)
{
    model_cfg m;
    memset(&m, 0, sizeof m);
    m.model_name = (char *)"test-model";
    m.temperature = 0.7;
    m.stream = 1;
    m.thinking = (char *)"high";

    msgs ms = {0};
    msgs_push(&ms, "system", "You are helpful.");
    msgs_push(&ms, "user", "hi\n\"there\"");

    dbuf b;
    dbuf_init(&b);
    build_body(&m, &ms, &b);

    const char *root = js_skip(b.data);
    char *model = js_get_string(root, "model");
    CHECK(model && strcmp(model, "test-model") == 0);
    free(model);

    const char *vs;
    CHECK(js_find(root, "messages", &vs, NULL) && *vs == '[');
    CHECK(js_find(root, "stream", &vs, NULL) && strncmp(vs, "true", 4) == 0);
    CHECK(js_find(root, "temperature", &vs, NULL) && strncmp(vs, "0.70", 4) == 0);
    char *te = js_get_string(root, "reasoning_effort");
    CHECK(te && strcmp(te, "high") == 0);
    free(te);
    /* 用户消息被正确转义 */
    CHECK(strstr(b.data, "hi\\n\\\"there\\\"") != NULL);

    dbuf_free(&b);
    msgs_free(&ms);
}

static void test_trim_context(void)
{
    msgs ms = {0};
    msgs_push(&ms, "system", "sys");
    char big[3001];
    memset(big, 'a', 3000);
    big[3000] = '\0';
    msgs_push(&ms, "user", big);
    msgs_push(&ms, "assistant", big);
    msgs_push(&ms, "user", "latest question");

    trim_to_context(&ms, 500); /* 估算约 2000 token，将触发裁剪 */
    CHECK(ms.n == 2);          /* 仅剩 system + 最新 user */
    CHECK(strcmp(ms.items[0].role, "system") == 0);
    CHECK(strcmp(ms.items[1].content, "latest question") == 0);

    /* context_size = 0 表示不限制 */
    msgs_push(&ms, "assistant", big);
    trim_to_context(&ms, 0);
    CHECK(ms.n == 3);

    msgs_free(&ms);
}

static void test_sse_line(void)
{
    sse_ctx c;
    memset(&c, 0, sizeof c);
    dbuf_init(&c.buf);
    dbuf_init(&c.raw);
    dbuf_init(&c.answer);
    dbuf_init(&c.reasoning);

    /* 用 dup2 把 stdout/stderr 暂时指向 /dev/null，避免污染测试输出 */
    int saved_out = dup(fileno(stdout));
    int saved_err = dup(fileno(stderr));
    int devnull = open("/dev/null", O_WRONLY);
    CHECK(saved_out >= 0 && saved_err >= 0 && devnull >= 0);
    dup2(devnull, fileno(stdout));
    dup2(devnull, fileno(stderr));

    handle_sse_line(": keep-alive comment", &c);
    handle_sse_line("event: message", &c);
    handle_sse_line("", &c);
    handle_sse_line("data: {\"choices\":[{\"delta\":{\"role\":\"assistant\",\"content\":\"\"}}]}", &c);
    handle_sse_line("data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"想一下\"}}]}", &c);
    handle_sse_line("data: {\"choices\":[{\"delta\":{\"content\":\"你好\"}}]}", &c);
    handle_sse_line("data: {\"choices\":[{\"delta\":{\"content\":\"，世界\"}}]}", &c);
    handle_sse_line("data: [DONE]", &c);

    fflush(stdout);
    fflush(stderr);
    dup2(saved_out, fileno(stdout));
    dup2(saved_err, fileno(stderr));
    close(devnull);
    close(saved_out);
    close(saved_err);

    CHECK(c.got_data == 1);
    CHECK(c.done == 1);
    CHECK(strcmp(c.answer.data, "你好，世界") == 0);
    CHECK(strcmp(c.reasoning.data, "想一下") == 0);

    dbuf_free(&c.buf);
    dbuf_free(&c.raw);
    dbuf_free(&c.answer);
    dbuf_free(&c.reasoning);
}

int main(void)
{
    test_split_requests();
    test_json_parse();
    test_escape_roundtrip();
    test_build_body();
    test_trim_context();
    test_sse_line();

    if (failures == 0) {
        printf("ALL TESTS PASSED\n");
        return 0;
    }
    printf("%d TEST(S) FAILED\n", failures);
    return 1;
}
