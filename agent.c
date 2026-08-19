/*
 * mini-agent —— 直接调用 OpenAI 兼容 API 的最小 agent 框架（单文件实现）
 *
 * 特性：
 *   - 仅依赖 libcurl；内置微型 JSON 构造/解析器，无其他第三方依赖
 *   - 支持 SSE 流式输出（stream=true）与普通整包返回（stream=false）
 *   - 配置文件（INI 风格）可定义任意多个模型：
 *       接入点 endpoint / api_key / 上下文大小 context_size / 温度 temperature /
 *       思考深度 thinking(映射为 reasoning_effort) / 流式开关 stream / 超时 timeout
 *   - -s 从文件加载系统提示词
 *   - -r 从文件批量加载用户请求（以连续 3 个换行符 \n\n\n 分隔），
 *     按顺序作为一次多轮对话依次执行，自动维护上下文并按 context_size 裁剪
 *
 * 输出约定：stdout 只输出模型回答；stderr 输出思考内容、状态与错误信息。
 *
 * 构建：见 Makefile（make）
 */

#define _POSIX_C_SOURCE 200809L

#include <ctype.h>
#include <getopt.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

#include <curl/curl.h>

/* ================================================================
 * 动态缓冲区
 * ================================================================ */

typedef struct {
    char *data; /* 始终以 NUL 结尾，可当 C 字符串使用 */
    size_t len, cap;
} dbuf;

static void dbuf_init(dbuf *b)
{
    b->cap = 64;
    b->len = 0;
    b->data = malloc(b->cap);
    if (!b->data) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
    b->data[0] = '\0';
}

static void dbuf_free(dbuf *b)
{
    free(b->data);
    b->data = NULL;
    b->len = b->cap = 0;
}

static void dbuf_reserve(dbuf *b, size_t extra)
{
    if (b->len + extra + 1 <= b->cap)
        return;
    while (b->cap < b->len + extra + 1)
        b->cap *= 2;
    b->data = realloc(b->data, b->cap);
    if (!b->data) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
}

static void dbuf_append(dbuf *b, const char *s, size_t n)
{
    dbuf_reserve(b, n);
    memcpy(b->data + b->len, s, n);
    b->len += n;
    b->data[b->len] = '\0';
}

static void dbuf_str(dbuf *b, const char *s)
{
    dbuf_append(b, s, strlen(s));
}

static void dbuf_printf(dbuf *b, const char *fmt, ...)
{
    va_list ap, ap2;
    va_start(ap, fmt);
    va_copy(ap2, ap);
    int n = vsnprintf(NULL, 0, fmt, ap);
    va_end(ap);
    if (n < 0) { va_end(ap2); return; }
    dbuf_reserve(b, (size_t)n);
    vsnprintf(b->data + b->len, (size_t)n + 1, fmt, ap2);
    va_end(ap2);
    b->len += (size_t)n;
}

/* 以 JSON 字符串转义规则追加 s（不含首尾引号） */
static void dbuf_json_escape(dbuf *b, const char *s)
{
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        switch (*p) {
        case '"':  dbuf_str(b, "\\\""); break;
        case '\\': dbuf_str(b, "\\\\"); break;
        case '\n': dbuf_str(b, "\\n");  break;
        case '\r': dbuf_str(b, "\\r");  break;
        case '\t': dbuf_str(b, "\\t");  break;
        case '\b': dbuf_str(b, "\\b");  break;
        case '\f': dbuf_str(b, "\\f");  break;
        default:
            if (*p < 0x20)
                dbuf_printf(b, "\\u%04x", *p);
            else
                dbuf_append(b, (const char *)p, 1); /* UTF-8 原样保留 */
        }
    }
}

/* ================================================================
 * 微型 JSON 解析器（只读、不构建语法树，只按 key 定位值）
 * 足以解析 chat completions 响应中的目标字段。
 * ================================================================ */

static const char *js_skip(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')
        p++;
    return p;
}

static const char *js_value_end(const char *p); /* 前置声明 */

/* p 指向开引号 '"'，返回闭引号后一字节；非法返回 NULL */
static const char *js_string_end(const char *p)
{
    if (*p != '"')
        return NULL;
    p++;
    while (*p) {
        if (*p == '\\') {
            if (!p[1])
                return NULL;
            p += 2; /* \uXXXX 中的十六进制字符不可能是 " 或 \，跳过两字节安全 */
        } else if (*p == '"') {
            return p + 1;
        } else {
            p++;
        }
    }
    return NULL;
}

/* p 指向 '{'，返回对象结束后一字节；非法返回 NULL */
static const char *js_object_end(const char *p)
{
    p++; /* skip '{' */
    for (;;) {
        p = js_skip(p);
        if (*p == '}')
            return p + 1;
        if (*p != '"')
            return NULL;
        const char *kend = js_string_end(p);
        if (!kend)
            return NULL;
        p = js_skip(kend);
        if (*p != ':')
            return NULL;
        p = js_skip(p + 1);
        const char *vend = js_value_end(p);
        if (!vend)
            return NULL;
        p = js_skip(vend);
        if (*p == ',') {
            p++;
            continue;
        }
        if (*p == '}')
            return p + 1;
        return NULL;
    }
}

/* p 指向 '['，返回数组结束后一字节；非法返回 NULL */
static const char *js_array_end(const char *p)
{
    p++; /* skip '[' */
    p = js_skip(p);
    if (*p == ']')
        return p + 1;
    for (;;) {
        const char *vend = js_value_end(p);
        if (!vend)
            return NULL;
        p = js_skip(vend);
        if (*p == ',') {
            p = js_skip(p + 1);
            continue;
        }
        if (*p == ']')
            return p + 1;
        return NULL;
    }
}

/* p 指向任意 JSON 值起点，返回该值结束后一字节；非法返回 NULL */
static const char *js_value_end(const char *p)
{
    p = js_skip(p);
    switch (*p) {
    case '"': return js_string_end(p);
    case '{': return js_object_end(p);
    case '[': return js_array_end(p);
    case 't': return strncmp(p, "true", 4) == 0 ? p + 4 : NULL;
    case 'f': return strncmp(p, "false", 5) == 0 ? p + 5 : NULL;
    case 'n': return strncmp(p, "null", 4) == 0 ? p + 4 : NULL;
    default:
        if (*p == '-' || isdigit((unsigned char)*p)) {
            const char *q = p;
            if (*q == '-')
                q++;
            if (!isdigit((unsigned char)*q))
                return NULL;
            while (isdigit((unsigned char)*q))
                q++;
            if (*q == '.') {
                q++;
                while (isdigit((unsigned char)*q))
                    q++;
            }
            if (*q == 'e' || *q == 'E') {
                q++;
                if (*q == '+' || *q == '-')
                    q++;
                while (isdigit((unsigned char)*q))
                    q++;
            }
            return q;
        }
        return NULL;
    }
}

/*
 * 在对象 obj（p 指向 '{'）的顶层查找 key。
 * 成功返回 1，并通过 vs/ve 输出值的起点与终点（两者均可为 NULL）。
 */
static int js_find(const char *obj, const char *key,
                   const char **vs, const char **ve)
{
    if (!obj || *obj != '{')
        return 0;
    size_t klen = strlen(key);
    const char *p = obj + 1;
    for (;;) {
        p = js_skip(p);
        if (*p != '"')
            return 0;
        const char *ks = p;
        const char *ke = js_string_end(p);
        if (!ke)
            return 0;
        p = js_skip(ke);
        if (*p != ':')
            return 0;
        p = js_skip(p + 1);
        const char *vend = js_value_end(p);
        if (!vend)
            return 0;
        /* 目标 key 均为普通 ASCII，键内不含转义时可直接比较 */
        if ((size_t)(ke - ks - 2) == klen && memcmp(ks + 1, key, klen) == 0) {
            if (vs) *vs = p;
            if (ve) *ve = vend;
            return 1;
        }
        p = js_skip(vend);
        if (*p == ',') {
            p++;
            continue;
        }
        return 0; /* '}' 或非法：未找到 */
    }
}

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static unsigned int hex4(const char *p)
{
    unsigned int v = 0;
    for (int i = 0; i < 4; i++) {
        int h = hexval((unsigned char)p[i]);
        if (h < 0)
            return 0xFFFD; /* 非法 \u，用替换字符 */
        v = (v << 4) | (unsigned)h;
    }
    return v;
}

/* 将 Unicode 码点编码为 UTF-8，返回写入字节数 */
static int utf8_encode(char *dst, unsigned int cp)
{
    if (cp < 0x80) {
        dst[0] = (char)cp;
        return 1;
    }
    if (cp < 0x800) {
        dst[0] = (char)(0xC0 | (cp >> 6));
        dst[1] = (char)(0x80 | (cp & 0x3F));
        return 2;
    }
    if (cp < 0x10000) {
        dst[0] = (char)(0xE0 | (cp >> 12));
        dst[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        dst[2] = (char)(0x80 | (cp & 0x3F));
        return 3;
    }
    dst[0] = (char)(0xF0 | (cp >> 18));
    dst[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
    dst[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
    dst[3] = (char)(0x80 | (cp & 0x3F));
    return 4;
}

/*
 * 将 JSON 字符串字面量 [s, e) 反转义为 malloc 的 UTF-8 字符串。
 * s 指向开引号，e 指向闭引号后一字节。支持 \uXXXX 与代理对。
 */
static char *js_unescape(const char *s, const char *e)
{
    size_t cap = (size_t)(e - s); /* 反转义只会变短或等长 */
    char *out = malloc(cap + 1);
    if (!out) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
    const char *p = s + 1;
    const char *stop = e - 1; /* 闭引号位置 */
    size_t o = 0;
    while (p < stop) {
        if (*p != '\\') {
            out[o++] = *p++;
            continue;
        }
        p++; /* skip '\\' */
        if (p >= stop)
            break; /* 畸形输入，到此为止 */
        switch (*p) {
        case '"':  out[o++] = '"';  p++; break;
        case '\\': out[o++] = '\\'; p++; break;
        case '/':  out[o++] = '/';  p++; break;
        case 'b':  out[o++] = '\b'; p++; break;
        case 'f':  out[o++] = '\f'; p++; break;
        case 'n':  out[o++] = '\n'; p++; break;
        case 'r':  out[o++] = '\r'; p++; break;
        case 't':  out[o++] = '\t'; p++; break;
        case 'u': {
            p++;
            if (stop - p < 4)
                goto done; /* 畸形，放弃剩余部分 */
            unsigned int cp = hex4(p);
            p += 4;
            /* 处理 UTF-16 代理对 \uD83D\uDE00 */
            if (cp >= 0xD800 && cp <= 0xDBFF && stop - p >= 6 &&
                p[0] == '\\' && p[1] == 'u') {
                unsigned int lo = hex4(p + 2);
                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    p += 6;
                }
            }
            o += (size_t)utf8_encode(out + o, cp);
            break;
        }
        default: /* 未知转义，原样保留 */
            out[o++] = *p++;
            break;
        }
    }
done:
    out[o] = '\0';
    return out;
}

/* 取对象 obj 中 key 对应的字符串值（malloc）；不存在或非字符串返回 NULL */
static char *js_get_string(const char *obj, const char *key)
{
    const char *vs, *ve;
    if (!js_find(obj, key, &vs, &ve))
        return NULL;
    if (*vs != '"') /* 例如 null / 数字 */
        return NULL;
    return js_unescape(vs, ve);
}

/* 从完整响应 JSON 中提取 choices[0].<inner>.<field> 字符串 */
static char *first_choice_field(const char *json, const char *inner,
                                const char *field)
{
    const char *root = js_skip(json);
    const char *vs;
    if (!js_find(root, "choices", &vs, NULL))
        return NULL;
    const char *arr = js_skip(vs);
    if (*arr != '[')
        return NULL;
    const char *elem = js_skip(arr + 1);
    if (*elem != '{')
        return NULL;
    const char *ds;
    if (!js_find(elem, inner, &ds, NULL))
        return NULL;
    return js_get_string(ds, field);
}

/* 从响应体中提取 error.message（malloc），无则返回 NULL */
static char *extract_error_message(const char *json)
{
    const char *root = js_skip(json);
    if (*root != '{')
        return NULL;
    const char *es, *ee;
    if (!js_find(root, "error", &es, &ee))
        return NULL;
    if (*es == '{')
        return js_get_string(es, "message");
    if (*es == '"')
        return js_unescape(es, ee);
    return NULL;
}

/* ================================================================
 * 文件读取 / 请求切分
 * ================================================================ */

static char *read_file(const char *path, size_t *len_out)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long sz = ftell(f);
    if (sz < 0) { fclose(f); return NULL; }
    rewind(f);
    char *buf = malloc((size_t)sz + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t rd = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    buf[rd] = '\0';
    if (len_out)
        *len_out = rd;
    return buf;
}

/* 就地归一化换行：\r\n 与孤立 \r 一律变为 \n */
static void normalize_newlines(char *s)
{
    char *r = s, *w = s;
    while (*r) {
        if (*r == '\r') {
            *w++ = '\n';
            if (r[1] == '\n')
                r++; /* \r\n 折叠为一个 \n */
        } else {
            *w++ = *r;
        }
        r++;
    }
    *w = '\0';
}

/* 取 [s, s+n) 去除首尾空白后的 malloc 副本；全空白返回 NULL */
static char *trim_dup(const char *s, size_t n)
{
    while (n > 0 && isspace((unsigned char)s[0])) { s++; n--; }
    while (n > 0 && isspace((unsigned char)s[n - 1])) n--;
    if (n == 0)
        return NULL;
    char *r = malloc(n + 1);
    if (!r) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
    memcpy(r, s, n);
    r[n] = '\0';
    return r;
}

/*
 * 将已归一化的文本按「连续 >= 3 个换行符」切分为请求列表。
 * 返回 malloc 数组（每项均为 malloc 字符串），个数写入 *count_out。
 */
static char **split_requests(const char *text, size_t *count_out)
{
    size_t cap = 8, n = 0;
    char **arr = malloc(cap * sizeof(char *));
    if (!arr) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
    size_t len = strlen(text);
    size_t i = 0, start = 0;

    #define PUSH_CHUNK(a, b)                                              \
        do {                                                              \
            char *c = trim_dup((a), (b));                                 \
            if (c) {                                                      \
                if (n == cap) {                                           \
                    cap *= 2;                                             \
                    arr = realloc(arr, cap * sizeof(char *));             \
                    if (!arr) {                                           \
                        fprintf(stderr, "[fatal] out of memory\n");       \
                        exit(1);                                          \
                    }                                                     \
                }                                                         \
                arr[n++] = c;                                             \
            }                                                             \
        } while (0)

    while (i < len) {
        if (text[i] == '\n') {
            size_t j = i;
            while (j < len && text[j] == '\n')
                j++;
            if (j - i >= 3) { /* 分隔符：3 个及以上连续换行 */
                PUSH_CHUNK(text + start, i - start);
                start = j;
            }
            i = j;
        } else {
            i++;
        }
    }
    PUSH_CHUNK(text + start, len - start);
    #undef PUSH_CHUNK

    *count_out = n;
    return arr;
}

/* ================================================================
 * 对话历史与上下文裁剪
 * ================================================================ */

typedef struct { char *role; char *content; } msg;

typedef struct {
    msg *items;
    size_t n, cap;
} msgs;

static void msgs_push(msgs *ms, const char *role, const char *content)
{
    if (ms->n == ms->cap) {
        ms->cap = ms->cap ? ms->cap * 2 : 8;
        ms->items = realloc(ms->items, ms->cap * sizeof(msg));
        if (!ms->items) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
    }
    ms->items[ms->n].role = strdup(role);
    ms->items[ms->n].content = strdup(content);
    ms->n++;
}

/* single-turn 模式用：丢弃全部非 system 消息，使每条请求的上下文相互独立 */
static void msgs_reset_to_system(msgs *ms)
{
    size_t i = 0;
    while (i < ms->n && strcmp(ms->items[i].role, "system") == 0)
        i++;
    for (size_t j = i; j < ms->n; j++) {
        free(ms->items[j].role);
        free(ms->items[j].content);
    }
    ms->n = i;
}

static void msgs_free(msgs *ms)
{
    for (size_t i = 0; i < ms->n; i++) {
        free(ms->items[i].role);
        free(ms->items[i].content);
    }
    free(ms->items);
    ms->items = NULL;
    ms->n = ms->cap = 0;
}

/*
 * 粗略 token 估算：按 3 字节/token（兼顾中英文），另计每条消息的固定开销。
 * 仅用于上下文裁剪，非精确计费。
 */
static size_t estimate_tokens(const msgs *ms)
{
    size_t bytes = 0;
    for (size_t i = 0; i < ms->n; i++)
        bytes += strlen(ms->items[i].role) + strlen(ms->items[i].content) + 12;
    return bytes / 3;
}

/* 超出 context_size 时，从最早的非 system 消息开始丢弃（system 始终保留） */
static void trim_to_context(msgs *ms, long context_size)
{
    if (context_size <= 0)
        return; /* 0 表示不限制 */
    for (;;) {
        size_t nonsys = 0;
        for (size_t i = 0; i < ms->n; i++)
            if (strcmp(ms->items[i].role, "system") != 0)
                nonsys++;
        if (nonsys <= 1 || estimate_tokens(ms) <= (size_t)context_size)
            break;
        size_t idx = (strcmp(ms->items[0].role, "system") == 0) ? 1 : 0;
        fprintf(stderr, "[context] 估算超出上下文窗口，丢弃最早消息（%s）\n",
                ms->items[idx].role);
        free(ms->items[idx].role);
        free(ms->items[idx].content);
        memmove(&ms->items[idx], &ms->items[idx + 1],
                (ms->n - idx - 1) * sizeof(msg));
        ms->n--;
    }
}

/* ================================================================
 * 配置文件（INI 风格）
 *
 *   [default]
 *   model = NAME
 *
 *   [model NAME]
 *   endpoint = https://host/v1/chat/completions   ; 完整接入点 URL
 *   api_key = sk-...            ; 或 env:VAR 从环境变量读取
 *   model = api-model-id        ; 发给 API 的 model 字段，默认同节名
 *   context_size = 64000        ; 上下文窗口(token)，0=不限制
 *   temperature = 0.7           ; <0 或注释掉 = 不发送该参数
 *   thinking = medium           ; 思考深度，原样作为 reasoning_effort 发送
 *   stream = true               ; SSE 流式开关
 *   timeout = 120               ; 请求超时（秒）
 * ================================================================ */

typedef struct {
    char *name;         /* 节名，供 -m 选择 */
    char *endpoint;     /* chat completions 完整 URL */
    char *api_key;
    char *model_name;   /* 发送给 API 的 model id */
    long context_size;  /* token；0 = 不限制 */
    double temperature; /* <0 = 不发送 */
    char *thinking;     /* NULL/空 = 不发送 reasoning_effort */
    int stream;         /* 1 = SSE 流式 */
    long timeout;       /* 秒 */
} model_cfg;

typedef struct {
    model_cfg *items;
    size_t n, cap;
    char *default_model;
} config;

static char *xstrdup(const char *s)
{
    char *r = strdup(s);
    if (!r) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
    return r;
}

static void set_str(char **dst, const char *v)
{
    free(*dst);
    *dst = xstrdup(v);
}

/* 就地去除首尾空白并返回新起点 */
static char *trim_inplace(char *s)
{
    while (isspace((unsigned char)*s))
        s++;
    char *e = s + strlen(s);
    while (e > s && isspace((unsigned char)e[-1]))
        *--e = '\0';
    return s;
}

static int parse_bool(const char *v, int dflt)
{
    if (!*v)
        return dflt;
    if (!strcasecmp(v, "true") || !strcasecmp(v, "yes") ||
        !strcasecmp(v, "on") || !strcmp(v, "1"))
        return 1;
    if (!strcasecmp(v, "false") || !strcasecmp(v, "no") ||
        !strcasecmp(v, "off") || !strcmp(v, "0"))
        return 0;
    return dflt;
}

static model_cfg *config_add_model(config *cfg, const char *name)
{
    if (cfg->n == cfg->cap) {
        cfg->cap = cfg->cap ? cfg->cap * 2 : 4;
        cfg->items = realloc(cfg->items, cfg->cap * sizeof(model_cfg));
        if (!cfg->items) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
    }
    model_cfg *m = &cfg->items[cfg->n++];
    memset(m, 0, sizeof *m);
    m->name = xstrdup(name);
    m->temperature = -1; /* 默认不发送 */
    m->stream = 1;       /* 默认流式 */
    m->timeout = 120;
    return m;
}

static model_cfg *config_find(config *cfg, const char *name)
{
    for (size_t i = 0; i < cfg->n; i++)
        if (strcmp(cfg->items[i].name, name) == 0)
            return &cfg->items[i];
    return NULL;
}

static void config_free(config *cfg)
{
    for (size_t i = 0; i < cfg->n; i++) {
        model_cfg *m = &cfg->items[i];
        free(m->name);
        free(m->endpoint);
        free(m->api_key);
        free(m->model_name);
        free(m->thinking);
    }
    free(cfg->items);
    free(cfg->default_model);
    memset(cfg, 0, sizeof *cfg);
}

static int load_config(config *cfg, const char *path)
{
    memset(cfg, 0, sizeof *cfg);
    char *text = read_file(path, NULL);
    if (!text) {
        fprintf(stderr, "[config] 无法打开配置文件: %s\n", path);
        return -1;
    }

    model_cfg *cur = NULL;
    int in_default = 0;

    for (char *line = strtok(text, "\n"); line; line = strtok(NULL, "\n")) {
        char *s = trim_inplace(line);
        if (!*s || *s == '#' || *s == ';')
            continue;

        if (*s == '[') { /* 节头 */
            char *rb = strchr(s, ']');
            if (!rb) {
                fprintf(stderr, "[config] 忽略非法节: %s\n", s);
                continue;
            }
            *rb = '\0';
            char *sec = trim_inplace(s + 1);
            cur = NULL;
            in_default = 0;
            if (strncmp(sec, "model", 5) == 0 &&
                (sec[5] == '\0' || sec[5] == ' ' || sec[5] == ':' || sec[5] == '\t')) {
                char *name = trim_inplace(sec + 5 + (sec[5] ? 1 : 0));
                if (*name) {
                    cur = config_add_model(cfg, name);
                } else {
                    fprintf(stderr, "[config] 节缺少模型名: [%s]\n", sec);
                }
            } else if (strcmp(sec, "default") == 0) {
                in_default = 1;
            } else {
                fprintf(stderr, "[config] 忽略未知节: [%s]\n", sec);
            }
            continue;
        }

        char *eq = strchr(s, '=');
        if (!eq) {
            fprintf(stderr, "[config] 忽略非法行: %s\n", s);
            continue;
        }
        *eq = '\0';
        char *key = trim_inplace(s);
        char *val = trim_inplace(eq + 1);

        if (in_default) {
            if (strcmp(key, "model") == 0)
                set_str(&cfg->default_model, val);
            else
                fprintf(stderr, "[config] 忽略 [default] 中未知键: %s\n", key);
            continue;
        }
        if (!cur)
            continue;

        if (strcmp(key, "endpoint") == 0) {
            set_str(&cur->endpoint, val);
        } else if (strcmp(key, "api_key") == 0) {
            if (strncmp(val, "env:", 4) == 0) {
                const char *env = getenv(val + 4);
                set_str(&cur->api_key, env ? env : "");
                if (!env)
                    fprintf(stderr, "[config] 警告: 环境变量 %s 未设置\n", val + 4);
            } else {
                set_str(&cur->api_key, val);
            }
        } else if (strcmp(key, "model") == 0) {
            set_str(&cur->model_name, val);
        } else if (strcmp(key, "context_size") == 0) {
            cur->context_size = strtol(val, NULL, 10);
        } else if (strcmp(key, "temperature") == 0) {
            cur->temperature = strtod(val, NULL);
        } else if (strcmp(key, "thinking") == 0) {
            set_str(&cur->thinking, val);
        } else if (strcmp(key, "stream") == 0) {
            cur->stream = parse_bool(val, 1);
        } else if (strcmp(key, "timeout") == 0) {
            cur->timeout = strtol(val, NULL, 10);
        } else {
            fprintf(stderr, "[config] 忽略未知键: %s\n", key);
        }
    }
    free(text);

    for (size_t i = 0; i < cfg->n; i++)
        if (!cfg->items[i].model_name)
            cfg->items[i].model_name = xstrdup(cfg->items[i].name);

    if (cfg->n == 0) {
        fprintf(stderr, "[config] 配置文件中没有定义任何 [model NAME] 节\n");
        return -1;
    }
    return 0;
}

/* ================================================================
 * 请求体构造
 * ================================================================ */

static void build_body(const model_cfg *m, const msgs *ms, dbuf *out)
{
    dbuf_str(out, "{\"model\":\"");
    dbuf_json_escape(out, m->model_name);
    dbuf_str(out, "\",\"messages\":[");
    for (size_t i = 0; i < ms->n; i++) {
        if (i)
            dbuf_str(out, ",");
        dbuf_str(out, "{\"role\":\"");
        dbuf_json_escape(out, ms->items[i].role);
        dbuf_str(out, "\",\"content\":\"");
        dbuf_json_escape(out, ms->items[i].content);
        dbuf_str(out, "\"}");
    }
    dbuf_str(out, "]");
    if (m->temperature >= 0)
        dbuf_printf(out, ",\"temperature\":%.2f", m->temperature);
    if (m->thinking && *m->thinking) {
        dbuf_str(out, ",\"reasoning_effort\":\"");
        dbuf_json_escape(out, m->thinking);
        dbuf_str(out, "\"");
    }
    if (m->stream)
        dbuf_str(out, ",\"stream\":true");
    dbuf_str(out, "}");
}

/* ================================================================
 * HTTP 调用与 SSE 解析
 * ================================================================ */

typedef struct {
    dbuf buf;       /* 尚未构成完整行的剩余字节 */
    dbuf raw;       /* 首个有效 data 块之前的原始字节（用于错误上报） */
    dbuf answer;    /* 累积的正式回答 */
    dbuf reasoning; /* 累积的思考内容 */
    int got_data;   /* 已解析出有效 data 块 */
    int done;       /* 收到 [DONE] */
    int reasoning_started, content_started;
} sse_ctx;

/* 处理一条完整的 SSE 行（不含行尾换行符） */
static void handle_sse_line(const char *line, sse_ctx *c)
{
    if (!*line || line[0] == ':')
        return; /* 空行 / SSE 注释 */
    if (strncmp(line, "data:", 5) != 0)
        return; /* 忽略 event:/id:/retry: 等字段 */

    const char *payload = js_skip(line + 5);
    if (strcmp(payload, "[DONE]") == 0) {
        c->done = 1;
        return;
    }
    if (*payload != '{')
        return;

    const char *es, *ee;
    if (js_find(payload, "error", &es, &ee)) { /* 流中错误块 */
        char *em = (*es == '{') ? js_get_string(es, "message")
                                : (*es == '"' ? js_unescape(es, ee) : NULL);
        fprintf(stderr, "\n[API 错误] %s\n", em ? em : "(无详情)");
        free(em);
        return;
    }

    const char *vs;
    if (!js_find(payload, "choices", &vs, NULL))
        return;
    const char *arr = js_skip(vs);
    if (*arr != '[')
        return;
    const char *elem = js_skip(arr + 1);
    if (*elem != '{')
        return;
    const char *ds;
    if (!js_find(elem, "delta", &ds, NULL))
        return;
    c->got_data = 1;

    /* 思考内容（如 DeepSeek reasoning_content）输出到 stderr */
    char *reason = js_get_string(ds, "reasoning_content");
    if (reason) {
        if (*reason) {
            if (!c->reasoning_started) {
                c->reasoning_started = 1;
                fputs("\n[thinking] ", stderr);
            }
            fputs(reason, stderr);
            fflush(stderr);
            dbuf_str(&c->reasoning, reason);
        }
        free(reason);
    }

    /* 正式回答输出到 stdout */
    char *piece = js_get_string(ds, "content");
    if (piece) {
        if (*piece) {
            if (c->reasoning_started && !c->content_started) {
                c->content_started = 1;
                fputs("\n[answer] ", stderr);
            }
            fputs(piece, stdout);
            fflush(stdout);
            dbuf_str(&c->answer, piece);
        }
        free(piece);
    }
}

/* libcurl 流式写回调：增量切行并解析 SSE */
static size_t stream_write_cb(char *ptr, size_t size, size_t nmemb, void *ud)
{
    sse_ctx *c = ud;
    size_t n = size * nmemb;
    if (!c->got_data)
        dbuf_append(&c->raw, ptr, n); /* 记录错误场景下的完整响应体 */
    dbuf_append(&c->buf, ptr, n);

    size_t start = 0;
    for (;;) {
        char *nl = memchr(c->buf.data + start, '\n', c->buf.len - start);
        if (!nl)
            break;
        size_t len = (size_t)(nl - (c->buf.data + start));
        char *line = malloc(len + 1);
        if (!line) { fprintf(stderr, "[fatal] out of memory\n"); exit(1); }
        memcpy(line, c->buf.data + start, len);
        line[len] = '\0';
        if (len > 0 && line[len - 1] == '\r')
            line[len - 1] = '\0';
        handle_sse_line(line, c);
        free(line);
        start = (size_t)(nl - c->buf.data) + 1;
    }
    if (start > 0) {
        memmove(c->buf.data, c->buf.data + start, c->buf.len - start);
        c->buf.len -= start;
        c->buf.data[c->buf.len] = '\0';
    }
    return n;
}

/* libcurl 写回调：整包收集（非流式） */
static size_t collect_write_cb(char *ptr, size_t size, size_t nmemb, void *ud)
{
    dbuf_append(ud, ptr, size * nmemb);
    return size * nmemb;
}

/*
 * 按当前对话历史调用一次模型。
 * 成功返回 0，*answer_out 为 malloc 的回答文本（调用方 free）；失败返回 -1。
 * 流式模式下回答会实时打印到 stdout（思考内容打印到 stderr）。
 */
static int call_model(const model_cfg *m, const msgs *history,
                      char **answer_out, int verbose)
{
    *answer_out = NULL;

    dbuf body;
    dbuf_init(&body);
    build_body(m, history, &body);
    if (verbose)
        fprintf(stderr, "\n[debug] 请求 JSON:\n%s\n", body.data);

    sse_ctx sc;
    memset(&sc, 0, sizeof sc);
    dbuf_init(&sc.buf);
    dbuf_init(&sc.raw);
    dbuf_init(&sc.answer);
    dbuf_init(&sc.reasoning);
    dbuf plain;
    dbuf_init(&plain);

    CURL *curl = curl_easy_init();
    if (!curl) {
        fprintf(stderr, "[fatal] curl_easy_init 失败\n");
        dbuf_free(&body); dbuf_free(&plain);
        dbuf_free(&sc.buf); dbuf_free(&sc.raw);
        dbuf_free(&sc.answer); dbuf_free(&sc.reasoning);
        return -1;
    }

    dbuf auth;
    dbuf_init(&auth);
    dbuf_printf(&auth, "Authorization: Bearer %s", m->api_key ? m->api_key : "");

    struct curl_slist *hdrs = NULL;
    hdrs = curl_slist_append(hdrs, "Content-Type: application/json");
    hdrs = curl_slist_append(hdrs, auth.data);
    if (m->stream)
        hdrs = curl_slist_append(hdrs, "Accept: text/event-stream");

    curl_easy_setopt(curl, CURLOPT_URL, m->endpoint);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, hdrs);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.data);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "mini-agent/1.0");
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, m->timeout > 0 ? m->timeout : 300L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    if (m->stream) {
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, stream_write_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &sc);
    } else {
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, collect_write_cb);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &plain);
    }

    CURLcode rc = curl_easy_perform(curl);
    long http = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http);
    curl_easy_cleanup(curl);
    curl_slist_free_all(hdrs);

    int ret = -1;
    if (rc != CURLE_OK) {
        fprintf(stderr, "[curl] %s\n", curl_easy_strerror(rc));
    } else if (http >= 400) {
        const char *errbody = m->stream ? sc.raw.data : plain.data;
        char *em = extract_error_message(errbody);
        fprintf(stderr, "[HTTP %ld] %s\n", http,
                em ? em : (*errbody ? errbody : "(空响应体)"));
        free(em);
    } else if (m->stream) {
        if (sc.buf.len > 0) /* 末尾无换行的残行 */
            handle_sse_line(sc.buf.data, &sc);
        if (!sc.got_data) {
            fprintf(stderr, "[warn] 未收到有效 SSE 数据，响应片段: %.300s\n",
                    sc.raw.data);
        } else {
            *answer_out = xstrdup(sc.answer.data);
            ret = 0;
        }
    } else {
        char *reason = first_choice_field(plain.data, "message", "reasoning_content");
        if (reason) {
            if (*reason)
                fprintf(stderr, "\n[thinking] %s\n", reason);
            free(reason);
        }
        char *content = first_choice_field(plain.data, "message", "content");
        if (content) {
            fputs(content, stdout);
            fflush(stdout);
            *answer_out = content;
            ret = 0;
        } else {
            char *em = extract_error_message(plain.data);
            if (em) {
                fprintf(stderr, "[error] %s\n", em);
            } else {
                fprintf(stderr, "[error] 无法解析响应: %.300s\n", plain.data);
            }
            free(em);
        }
    }

    dbuf_free(&body);
    dbuf_free(&auth);
    dbuf_free(&plain);
    dbuf_free(&sc.buf);
    dbuf_free(&sc.raw);
    dbuf_free(&sc.answer);
    dbuf_free(&sc.reasoning);
    return ret;
}

/* ================================================================
 * 主流程
 * ================================================================ */

#ifndef AGENT_NO_MAIN

static void usage(int code)
{
    FILE *o = code == 0 ? stdout : stderr;
    fprintf(o,
        "mini-agent —— 调用 OpenAI 兼容 API 的最小 agent 框架\n"
        "\n"
        "用法: mini-agent -r 请求文件 [选项]\n"
        "\n"
        "选项:\n"
        "  -c FILE   模型配置文件            (默认 ./config.ini)\n"
        "  -m NAME   本次使用的模型          (默认取配置 [default] 的 model)\n"
        "  -s FILE   系统提示词文件          (可选)\n"
        "  -r FILE   批量用户请求文件        (必填；请求以 3 个换行符分隔)\n"
        "  -n        演练模式：只打印将发送的请求 JSON，不发起网络请求\n"
        "  --single-turn  单轮模式：每条请求使用独立上下文（仅 system + 当前请求），\n"
        "                 适用于批量评估等需要避免多轮相互影响的场景\n"
        "  -v        打印请求体等调试信息\n"
        "  -h        显示本帮助\n"
        "\n"
        "输出约定: stdout 仅输出模型回答；stderr 输出思考内容与状态信息。\n");
    exit(code);
}

static void print_preview(const char *s)
{
    size_t n = strlen(s);
    if (n <= 500)
        fprintf(stderr, "%s\n", s);
    else
        fprintf(stderr, "%.500s …(共 %zu 字符)\n", s, n);
}

int main(int argc, char **argv)
{
    const char *cfg_path = "config.ini";
    const char *sys_path = NULL, *req_path = NULL, *use_model = NULL;
    int dry_run = 0, verbose = 0, single_turn = 0;
    int opt;
    static const struct option long_opts[] = {
        {"single-turn", no_argument, NULL, 1001},
        {NULL, 0, NULL, 0}
    };

    while ((opt = getopt_long(argc, argv, "c:m:s:r:nvh", long_opts, NULL)) != -1) {
        switch (opt) {
        case 'c': cfg_path = optarg; break;
        case 'm': use_model = optarg; break;
        case 's': sys_path = optarg; break;
        case 'r': req_path = optarg; break;
        case 'n': dry_run = 1; break;
        case 'v': verbose = 1; break;
        case 1001: single_turn = 1; break;
        case 'h': usage(0); break;
        default:  usage(1);
        }
    }
    if (!req_path) {
        fprintf(stderr, "缺少 -r 请求文件\n\n");
        usage(1);
    }

    /* 1. 加载配置，选定模型 */
    config cfg;
    if (load_config(&cfg, cfg_path) != 0)
        return 1;
    const char *mname = use_model ? use_model : cfg.default_model;
    if (!mname)
        mname = cfg.items[0].name; /* 兜底：第一个模型 */
    model_cfg *m = config_find(&cfg, mname);
    if (!m) {
        fprintf(stderr, "未找到模型 '%s'，可用模型:\n", mname);
        for (size_t i = 0; i < cfg.n; i++)
            fprintf(stderr, "  - %s\n", cfg.items[i].name);
        return 1;
    }
    if (!m->endpoint || !*m->endpoint) {
        fprintf(stderr, "模型 '%s' 未配置 endpoint\n", m->name);
        return 1;
    }
    fprintf(stderr, "[mini-agent] 模型=%s  endpoint=%s  stream=%s  上下文=%s\n",
            m->name, m->endpoint, m->stream ? "开" : "关",
            single_turn ? "单轮独立" : "多轮累积");

    /* 2. 系统提示词（可选） */
    msgs history = {0};
    if (sys_path) {
        char *sp = read_file(sys_path, NULL);
        if (!sp) {
            fprintf(stderr, "无法读取系统提示词文件: %s\n", sys_path);
            return 1;
        }
        normalize_newlines(sp);
        char *st = trim_inplace(sp);
        if (*st)
            msgs_push(&history, "system", st);
        free(sp);
    }

    /* 3. 批量请求文件 */
    char *reqtext = read_file(req_path, NULL);
    if (!reqtext) {
        fprintf(stderr, "无法读取请求文件: %s\n", req_path);
        return 1;
    }
    normalize_newlines(reqtext);
    size_t nreq = 0;
    char **reqs = split_requests(reqtext, &nreq);
    if (nreq == 0) {
        fprintf(stderr, "请求文件中没有有效请求（请求之间请用 3 个换行符分隔）\n");
        return 1;
    }
    fprintf(stderr, "[mini-agent] 共 %zu 个请求\n", nreq);

    /* 4. 逐条执行，维护多轮上下文 */
    curl_global_init(CURL_GLOBAL_DEFAULT);
    int exit_code = 0;
    for (size_t i = 0; i < nreq; i++) {
        fprintf(stderr, "\n──────── 请求 %zu/%zu ────────\n", i + 1, nreq);
        print_preview(reqs[i]);
        fprintf(stderr, "──────── 回答 ────────\n");

        if (single_turn)
            msgs_reset_to_system(&history); /* 独立上下文：仅保留 system + 当前请求 */

        msgs_push(&history, "user", reqs[i]);
        trim_to_context(&history, m->context_size);

        if (dry_run) {
            dbuf body;
            dbuf_init(&body);
            build_body(m, &history, &body);
            fprintf(stderr, "(dry-run) 请求 JSON（%zu 字节）:\n%s\n",
                    body.len, body.data);
            dbuf_free(&body);
            msgs_push(&history, "assistant", "(dry-run 占位回答)");
            continue;
        }

        char *answer = NULL;
        if (call_model(m, &history, &answer, verbose) == 0) {
            size_t L = answer ? strlen(answer) : 0;
            if (L == 0 || answer[L - 1] != '\n')
                putchar('\n');
            fflush(stdout);
            msgs_push(&history, "assistant", answer ? answer : "");
        } else {
            fprintf(stderr, "[mini-agent] 请求 %zu 调用失败，继续下一条\n", i + 1);
            exit_code = 1;
        }
        free(answer);
    }
    curl_global_cleanup();

    /* 5. 清理 */
    for (size_t i = 0; i < nreq; i++)
        free(reqs[i]);
    free(reqs);
    free(reqtext);
    msgs_free(&history);
    config_free(&cfg);
    return exit_code;
}

#endif /* AGENT_NO_MAIN */
