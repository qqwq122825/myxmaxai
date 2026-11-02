//
//  RGConnection.m
//  AIMoYan
//

#import "RGConnection.h"
#import <CommonCrypto/CommonDigest.h>
#import <QuartzCore/QuartzCore.h>
#import <Security/Security.h>

static NSString *const RGErrorDomain = @"AIMoYan.Connection";
static const NSInteger RGDefaultPort = 8766;
static const NSInteger RGMaxInFlight = 3;          // queued video messages before we start dropping camera frames
static const NSInteger RGMaxMessage = 4 * 1024 * 1024;

static NSError *RGError(NSString *message) {
    return [NSError errorWithDomain:RGErrorDomain code:1 userInfo:@{NSLocalizedDescriptionKey: message}];
}

#pragma mark - RGServerInfo

@interface RGServerInfo ()
@property (nonatomic, copy, readwrite) NSString *host;
@property (nonatomic, readwrite) NSInteger port;
@property (nonatomic, copy, readwrite) NSString *secret;
@property (nonatomic, copy, readwrite) NSData *fingerprint;
- (NSString *)authority;
@end

@implementation RGServerInfo

+ (nullable NSData *)base64URLDecode:(NSString *)text {
    NSMutableString *s = [[text stringByReplacingOccurrencesOfString:@"-" withString:@"+"] mutableCopy];
    [s replaceOccurrencesOfString:@"_" withString:@"/" options:0 range:NSMakeRange(0, s.length)];
    while (s.length % 4 != 0) [s appendString:@"="];
    return [[NSData alloc] initWithBase64EncodedString:s options:0];
}

+ (nullable instancetype)infoWithAddress:(NSString *)address key:(NSString *)key error:(NSError **)error {
    RGServerInfo *info = [RGServerInfo new];

    // ---- Address ----
    NSString *a = [address stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    for (NSString *scheme in @[@"https://", @"wss://", @"http://", @"ws://"]) {
        if ([a.lowercaseString hasPrefix:scheme]) { a = [a substringFromIndex:scheme.length]; break; }
    }
    NSRange slash = [a rangeOfString:@"/"];
    if (slash.location != NSNotFound) a = [a substringToIndex:slash.location];
    NSString *host = a; NSInteger port = RGDefaultPort;
    if ([a hasPrefix:@"["]) {                                   // [ipv6]:port
        NSRange close = [a rangeOfString:@"]"];
        if (close.location == NSNotFound) { if (error) *error = RGError(@"服务器地址格式不正确。"); return nil; }
        host = [a substringWithRange:NSMakeRange(1, close.location - 1)];
        NSString *rest = [a substringFromIndex:close.location + 1];
        if ([rest hasPrefix:@":"]) port = [rest substringFromIndex:1].integerValue;
    } else {
        NSArray<NSString *> *parts = [a componentsSeparatedByString:@":"];
        if (parts.count == 2) { host = parts[0]; port = parts[1].integerValue; }
        else if (parts.count > 2) { host = a; }                 // bare IPv6 without port
    }
    if (host.length == 0) { if (error) *error = RGError(@"请填写服务器地址，例如 1.2.3.4:8766。"); return nil; }
    if (port < 1 || port > 65535) { if (error) *error = RGError(@"端口须为 1–65535。"); return nil; }
    info.host = host; info.port = port;

    // ---- Key: "RGK1." + base64url( 32-byte cert SHA-256 || ascii bearer key ) ----
    NSString *k = [key stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    if (![k hasPrefix:@"RGK1."] || k.length > 512) {
        if (error) *error = RGError(@"连接 Key 无效，请复制服务器上完整的 Key（RGK1. 开头）。");
        return nil;
    }
    NSData *payload = [self base64URLDecode:[k substringFromIndex:5]];
    if (payload.length < 32 + 24) {
        if (error) *error = RGError(@"连接 Key 不完整，请重新复制。");
        return nil;
    }
    info.fingerprint = [payload subdataWithRange:NSMakeRange(0, 32)];
    NSString *secret = [[NSString alloc] initWithData:[payload subdataWithRange:NSMakeRange(32, payload.length - 32)]
                                             encoding:NSASCIIStringEncoding];
    if (secret.length < 24 || secret.length > 256 ||
        [secret rangeOfCharacterFromSet:NSCharacterSet.whitespaceAndNewlineCharacterSet].location != NSNotFound) {
        if (error) *error = RGError(@"连接 Key 内容无效，请重新复制。");
        return nil;
    }
    info.secret = secret;
    return info;
}

- (NSString *)authority {
    return [self.host containsString:@":"] ? [NSString stringWithFormat:@"[%@]:%ld", self.host, (long)self.port]
                                           : [NSString stringWithFormat:@"%@:%ld", self.host, (long)self.port];
}

@end

#pragma mark - RGVideoSettings

@implementation RGVideoSettings
- (NSDictionary *)dictionary {
    return @{@"width": @(self.width), @"height": @(self.height), @"fps": @(self.fps), @"bitrate_kbps": @(self.bitrateKbps)};
}
@end

#pragma mark - RGConnection

@interface RGConnection () <NSURLSessionDelegate, NSURLSessionTaskDelegate, NSURLSessionWebSocketDelegate>
@property (nonatomic, strong) RGServerInfo *server;
@property (nonatomic, strong) RGVideoSettings *settings;
@property (nonatomic, strong, nullable) NSURLSession *session;
@property (nonatomic, strong, nullable) NSURLSessionWebSocketTask *socket;
@property (nonatomic, strong, nullable) dispatch_source_t pingTimer;
@property (nonatomic, readwrite) BOOL ready;
@property (nonatomic, readwrite) BOOL returnActive;
@property (atomic, readwrite) double rttMs;
@property (atomic, readwrite) uint64_t sentFrames, sentBytes, receivedFrames, receivedBytes, droppedFrames;
@property (atomic) NSInteger inFlight;
@property (atomic) BOOL closed;
@property (atomic) BOOL pinMismatch;
@end

@implementation RGConnection

- (instancetype)initWithServer:(RGServerInfo *)server settings:(RGVideoSettings *)settings {
    if ((self = [super init])) {
        _server = server;
        _settings = settings;
        _rttMs = -1;
    }
    return self;
}

- (NSMutableURLRequest *)requestForPath:(NSString *)path scheme:(NSString *)scheme {
    NSURL *url = [NSURL URLWithString:[NSString stringWithFormat:@"%@://%@%@", scheme, self.server.authority, path]];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    [request setValue:[@"Bearer " stringByAppendingString:self.server.secret] forHTTPHeaderField:@"Authorization"];
    request.timeoutInterval = 15;
    return request;
}

- (void)connect {
    NSURLSessionConfiguration *config = NSURLSessionConfiguration.ephemeralSessionConfiguration;
    config.connectionProxyDictionary = @{};          // direct connection, like the PC client (trust_env=False)
    config.timeoutIntervalForRequest = 20;
    config.waitsForConnectivity = NO;
    self.session = [NSURLSession sessionWithConfiguration:config delegate:self delegateQueue:nil];

    // 1) /health: verifies the pinned identity and the Key before opening the video channel.
    __weak typeof(self) weakSelf = self;
    NSURLSessionDataTask *health = [self.session dataTaskWithRequest:[self requestForPath:@"/health" scheme:@"https"]
        completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        __strong typeof(weakSelf) self = weakSelf;
        if (!self || self.closed) return;
        if (error) { [self failWith:[self describeTransportError:error]]; return; }
        NSInteger status = [(NSHTTPURLResponse *)response statusCode];
        if (status == 401) { [self failWith:RGError(@"Key 不正确，请在服务器上重新复制 Key。")]; return; }
        if (status != 200) { [self failWith:RGError([NSString stringWithFormat:@"服务器拒绝连接（HTTP %ld）。", (long)status])]; return; }
        NSDictionary *json = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if (![json isKindOfClass:NSDictionary.class] || ![json[@"protocol"] isEqual:@"direct-1"]) {
            [self failWith:RGError(@"服务器版本不兼容，请更新服务器程序。")]; return;
        }
        NSArray *features = [json[@"features"] isKindOfClass:NSArray.class] ? json[@"features"] : @[];
        if (![features containsObject:@"relay-roles"]) {
            [self failWith:RGError(@"服务器版本过旧，不支持手机推流。请先更新服务器程序。")]; return;
        }
        if ([json[@"busy"] boolValue]) { [self failWith:RGError(@"服务器已有其他推流设备连接，请先停止旧连接。")]; return; }
        [self openStream];
    }];
    [health resume];
}

- (void)openStream {
    NSMutableURLRequest *request = [self requestForPath:@"/stream" scheme:@"wss"];
    self.socket = [self.session webSocketTaskWithRequest:request];
    self.socket.maximumMessageSize = RGMaxMessage;
    [self.socket resume];

    NSDictionary *start = @{@"type": @"start", @"protocol": @"direct-1", @"role": @"source",
                            @"video": self.settings.dictionary};
    [self sendJSON:start];

    __weak typeof(self) weakSelf = self;
    [self.socket receiveMessageWithCompletionHandler:^(NSURLSessionWebSocketMessage *message, NSError *error) {
        __strong typeof(weakSelf) self = weakSelf;
        if (!self || self.closed) return;
        if (error) { [self failWith:[self describeTransportError:error]]; return; }
        NSDictionary *ready = [self parseControl:message];
        if ([ready[@"type"] isEqual:@"error"]) {
            [self failWith:RGError(ready[@"message"] ?: @"服务器初始化失败。")]; return;
        }
        if (![ready[@"type"] isEqual:@"ready"] || ![ready[@"protocol"] isEqual:@"direct-1"]) {
            [self failWith:RGError(@"服务器返回的确认信息不正确。")]; return;
        }
        BOOL active = NO;   // push-only: the processed picture goes to the PC, not back to the phone
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self.closed) return;
            self.ready = YES;
            [self.delegate connectionDidBecomeReady:self];
            [self setReturnActiveOnMain:active];
        });
        [self startPing];
        [self receiveLoop];
    }];
}

- (void)receiveLoop {
    __weak typeof(self) weakSelf = self;
    [self.socket receiveMessageWithCompletionHandler:^(NSURLSessionWebSocketMessage *message, NSError *error) {
        __strong typeof(weakSelf) self = weakSelf;
        if (!self || self.closed) return;
        if (error) { [self failWith:[self describeTransportError:error]]; return; }
        if (message.type == NSURLSessionWebSocketMessageTypeData) {
            self.receivedBytes += message.data.length;   // not expected in push-only mode; ignored
        } else {
            NSDictionary *control = [self parseControl:message];
            NSString *type = control[@"type"];
            if ([type isEqual:@"pong"] && [control[@"time"] isKindOfClass:NSNumber.class]) {
                self.rttMs = MAX(0, (CACurrentMediaTime() - [control[@"time"] doubleValue]) * 1000.0);
            } else if ([type isEqual:@"return_state"]) {
                BOOL active = [control[@"active"] boolValue];
                dispatch_async(dispatch_get_main_queue(), ^{ [self setReturnActiveOnMain:active]; });
            } else if ([type isEqual:@"error"]) {
                [self failWith:RGError(control[@"message"] ?: @"服务器处理失败。")]; return;
            }
        }
        [self receiveLoop];
    }];
}

- (void)setReturnActiveOnMain:(BOOL)active {
    if (self.closed) return;
    self.returnActive = active;
    [self.delegate connection:self returnActiveChanged:active];
}

- (nullable NSDictionary *)parseControl:(NSURLSessionWebSocketMessage *)message {
    if (message.type != NSURLSessionWebSocketMessageTypeString || message.string.length > 8192) return nil;
    NSData *data = [message.string dataUsingEncoding:NSUTF8StringEncoding];
    id json = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [json isKindOfClass:NSDictionary.class] ? json : nil;
}

- (void)sendJSON:(NSDictionary *)object {
    NSData *data = [NSJSONSerialization dataWithJSONObject:object options:0 error:nil];
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    __weak typeof(self) weakSelf = self;
    [self.socket sendMessage:[[NSURLSessionWebSocketMessage alloc] initWithString:text] completionHandler:^(NSError *error) {
        if (error) [weakSelf failWith:[weakSelf describeTransportError:error]];
    }];
}

- (void)startPing {
    dispatch_source_t timer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_global_queue(QOS_CLASS_UTILITY, 0));
    dispatch_source_set_timer(timer, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), NSEC_PER_SEC, NSEC_PER_SEC / 10);
    __weak typeof(self) weakSelf = self;
    dispatch_source_set_event_handler(timer, ^{
        __strong typeof(weakSelf) self = weakSelf;
        if (!self || self.closed) return;
        [self sendJSON:@{@"type": @"ping", @"time": @(CACurrentMediaTime())}];
    });
    self.pingTimer = timer;
    dispatch_resume(timer);
}

#pragma mark Video

- (BOOL)canSendVideo {
    return self.ready && !self.closed && self.inFlight < RGMaxInFlight;
}

- (void)noteDroppedFrame {
    self.droppedFrames += 1;
}

- (void)sendVideoMessage:(NSData *)message {
    if (self.closed || !self.socket) return;
    @synchronized (self) { self.inFlight += 1; }
    self.sentBytes += message.length;
    self.sentFrames += 1;
    __weak typeof(self) weakSelf = self;
    [self.socket sendMessage:[[NSURLSessionWebSocketMessage alloc] initWithData:message] completionHandler:^(NSError *error) {
        __strong typeof(weakSelf) self = weakSelf;
        if (!self) return;
        @synchronized (self) { self.inFlight -= 1; }
        if (error) [self failWith:[self describeTransportError:error]];
    }];
}

#pragma mark Closing

- (void)disconnect {
    [self closeWithError:nil];
}

- (void)failWith:(NSError *)error {
    [self closeWithError:error];
}

- (void)closeWithError:(nullable NSError *)error {
    @synchronized (self) {
        if (self.closed) return;
        self.closed = YES;
    }
    if (self.pingTimer) { dispatch_source_cancel(self.pingTimer); self.pingTimer = nil; }
    [self.socket cancelWithCloseCode:NSURLSessionWebSocketCloseCodeNormalClosure reason:nil];
    [self.session invalidateAndCancel];
    dispatch_async(dispatch_get_main_queue(), ^{
        self.ready = NO;
        [self.delegate connection:self didCloseWithError:error];
    });
}

- (NSError *)describeTransportError:(NSError *)error {
    if (self.pinMismatch) return RGError(@"服务器身份与 Key 不一致。请在服务器上重新复制 Key，或检查地址是否填错。");
    if ([error.domain isEqual:NSURLErrorDomain]) {
        switch (error.code) {
            case NSURLErrorCannotConnectToHost:
            case NSURLErrorCannotFindHost:
            case NSURLErrorTimedOut:
                return RGError(@"连不上服务器。请检查地址和端口、服务器是否已点“启动接收”、安全组和防火墙是否放行该端口。");
            case NSURLErrorNotConnectedToInternet:
            case NSURLErrorNetworkConnectionLost:
                return RGError(@"网络断开了，请检查手机网络后重试。");
            case NSURLErrorBadServerResponse:
                return RGError(@"服务器拒绝了连接（可能 Key 不对，或已有其他设备连接）。");
            default: break;
        }
    }
    return RGError([NSString stringWithFormat:@"连接中断：%@", error.localizedDescription]);
}

#pragma mark TLS pinning

- (void)handleChallenge:(NSURLAuthenticationChallenge *)challenge
      completionHandler:(void (^)(NSURLSessionAuthChallengeDisposition, NSURLCredential * _Nullable))completionHandler {
    if (![challenge.protectionSpace.authenticationMethod isEqual:NSURLAuthenticationMethodServerTrust]) {
        completionHandler(NSURLSessionAuthChallengePerformDefaultHandling, nil);
        return;
    }
    SecTrustRef trust = challenge.protectionSpace.serverTrust;
    CFArrayRef chain = trust ? SecTrustCopyCertificateChain(trust) : NULL;
    BOOL match = NO;
    if (chain && CFArrayGetCount(chain) > 0) {
        SecCertificateRef leaf = (SecCertificateRef)CFArrayGetValueAtIndex(chain, 0);
        NSData *der = CFBridgingRelease(SecCertificateCopyData(leaf));
        unsigned char digest[CC_SHA256_DIGEST_LENGTH];
        CC_SHA256(der.bytes, (CC_LONG)der.length, digest);
        // Constant-time comparison against the fingerprint carried in the Key.
        const unsigned char *expected = self.server.fingerprint.bytes;
        unsigned char diff = 0;
        for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) diff |= (unsigned char)(digest[i] ^ expected[i]);
        match = (diff == 0);
    }
    if (chain) CFRelease(chain);
    if (match) {
        // The server uses a self-signed certificate; it is trusted because it is exactly the one in the Key.
        completionHandler(NSURLSessionAuthChallengeUseCredential, [NSURLCredential credentialForTrust:trust]);
    } else {
        self.pinMismatch = YES;
        completionHandler(NSURLSessionAuthChallengeCancelAuthenticationChallenge, nil);
    }
}

- (void)URLSession:(NSURLSession *)session didReceiveChallenge:(NSURLAuthenticationChallenge *)challenge
 completionHandler:(void (^)(NSURLSessionAuthChallengeDisposition, NSURLCredential * _Nullable))completionHandler {
    [self handleChallenge:challenge completionHandler:completionHandler];
}

- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task didReceiveChallenge:(NSURLAuthenticationChallenge *)challenge
 completionHandler:(void (^)(NSURLSessionAuthChallengeDisposition, NSURLCredential * _Nullable))completionHandler {
    [self handleChallenge:challenge completionHandler:completionHandler];
}

- (void)URLSession:(NSURLSession *)session webSocketTask:(NSURLSessionWebSocketTask *)webSocketTask
  didCloseWithCode:(NSURLSessionWebSocketCloseCode)closeCode reason:(NSData *)reason {
    if (!self.closed) [self failWith:RGError(@"服务器已结束连接。")];
}

@end
