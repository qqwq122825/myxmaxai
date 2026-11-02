//
//  RGPairing.m
//  AIMoYan
//

#import "RGPairing.h"
#include <arpa/inet.h>

static NSError *RGPairingError(NSString *message) {
    return [NSError errorWithDomain:@"AIMoYan.Pairing" code:1 userInfo:@{NSLocalizedDescriptionKey: message}];
}

NSArray<NSString *> *RGSplitAddresses(NSString *text) {
    NSCharacterSet *separators = [NSCharacterSet characterSetWithCharactersInString:@",，;；\n "];
    NSMutableArray *result = [NSMutableArray array];
    for (NSString *part in [text componentsSeparatedByCharactersInSet:separators]) {
        NSString *a = [part stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        if (a.length && ![result containsObject:a]) [result addObject:a];
    }
    return result;
}

BOOL RGIsLocalHost(NSString *host) {
    NSString *h = host.lowercaseString;
    if ([h hasPrefix:@"["]) h = [h substringWithRange:NSMakeRange(1, MAX(0, (NSInteger)h.length - 2))];
    if ([h hasSuffix:@".local"]) return YES;
    struct in_addr v4;
    if (inet_pton(AF_INET, h.UTF8String, &v4) == 1) {
        uint32_t ip = ntohl(v4.s_addr);
        return (ip >> 24) == 10 || (ip >> 20) == ((172u << 4) | 1) || (ip >> 16) == ((192u << 8) | 168) ||
               (ip >> 16) == ((169u << 8) | 254);
    }
    struct in6_addr v6;
    if (inet_pton(AF_INET6, h.UTF8String, &v6) == 1) {
        return (v6.s6_addr[0] & 0xFE) == 0xFC || (v6.s6_addr[0] == 0xFE && (v6.s6_addr[1] & 0xC0) == 0x80);
    }
    return NO;
}

/// Host part of "host:port" / "[v6]:port" / "https://host:port/…".
static NSString *RGHostOf(NSString *address) {
    NSString *a = address;
    NSRange scheme = [a rangeOfString:@"://"];
    if (scheme.location != NSNotFound) a = [a substringFromIndex:NSMaxRange(scheme)];
    if ([a hasPrefix:@"["]) {
        NSRange close = [a rangeOfString:@"]"];
        return close.location == NSNotFound ? a : [a substringWithRange:NSMakeRange(1, close.location - 1)];
    }
    NSArray *parts = [a componentsSeparatedByString:@":"];
    return parts.count == 2 ? parts[0] : a;
}

@interface RGPairing ()
@property (nonatomic, readwrite) RGLinkMode mode;
@property (nonatomic, readwrite) BOOL hasMode;
@property (nonatomic, copy, readwrite) NSArray<NSString *> *addresses;
@property (nonatomic, copy, readwrite) NSString *key;
@property (nonatomic, copy, readwrite, nullable) NSString *name;
@end

@implementation RGPairing

+ (nullable instancetype)pairingFromString:(NSString *)text error:(NSError **)error {
    NSString *t = [text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    RGPairing *pairing = [RGPairing new];

    if ([t hasPrefix:@"RGK1."]) {                       // bare Key
        pairing.key = t;
        pairing.addresses = @[];
        return pairing;
    }

    NSURLComponents *url = [NSURLComponents componentsWithString:t];
    if (!url) {   // receiver forgot to percent-encode (e.g. a Chinese computer name)
        NSString *escaped = [t stringByAddingPercentEncodingWithAllowedCharacters:NSCharacterSet.URLQueryAllowedCharacterSet];
        if (escaped) url = [NSURLComponents componentsWithString:escaped];
    }
    if (!url || ![url.scheme.lowercaseString isEqual:@"aimoyan"] || ![url.host.lowercaseString isEqual:@"connect"]) {
        if (error) *error = RGPairingError(@"这不是 AI魔颜 的连接二维码。请扫描电脑接收端或服务器上显示的二维码。");
        return nil;
    }
    NSMutableDictionary<NSString *, NSString *> *query = [NSMutableDictionary dictionary];
    for (NSURLQueryItem *item in url.queryItems) {
        if (item.value) query[item.name.lowercaseString] = item.value;
    }
    NSInteger version = query[@"v"] ? query[@"v"].integerValue : 1;
    if (version > 1) {
        if (error) *error = RGPairingError(@"二维码来自更新版本的接收端，请先更新手机 App。");
        return nil;
    }
    pairing.key = [query[@"key"] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet] ?: @"";
    pairing.addresses = RGSplitAddresses(query[@"addr"] ?: @"");
    pairing.name = query[@"name"].length ? query[@"name"] : nil;
    if (![pairing.key hasPrefix:@"RGK1."] || pairing.addresses.count == 0) {
        if (error) *error = RGPairingError(@"二维码内容不完整（缺少地址或 Key），请在接收端刷新二维码后重扫。");
        return nil;
    }
    NSString *mode = query[@"mode"].lowercaseString;
    if ([mode isEqual:@"lan"]) pairing.mode = RGLinkModeLAN;
    else if ([mode isEqual:@"remote"]) pairing.mode = RGLinkModeRemote;
    else pairing.mode = RGIsLocalHost(RGHostOf(pairing.addresses.firstObject)) ? RGLinkModeLAN : RGLinkModeRemote;
    pairing.hasMode = YES;
    return pairing;
}

- (NSString *)addressText {
    return [self.addresses componentsJoinedByString:@", "];
}

@end
