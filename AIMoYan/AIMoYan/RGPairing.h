//
//  RGPairing.h
//  AIMoYan
//
//  Pairing info carried by the QR code the receiver shows (PC LAN receiver or remote server):
//
//    aimoyan://connect?v=1&mode=lan&addr=192.168.1.23:8766,10.0.0.5:8766&key=RGK1.xxxx&name=DESKTOP-01
//
//  mode  lan | remote          (missing -> guessed from the address: private IP = lan)
//  addr  one or more host:port, comma separated, tried in order (a PC may have several network cards)
//  key   the RGK1 Key (certificate fingerprint + bearer key), same as typed by hand
//  name  optional display name of the receiver
//
//  A QR that only holds "RGK1.xxxx" fills the Key alone.
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

typedef NS_ENUM(NSInteger, RGLinkMode) {
    RGLinkModeRemote = 0,     // 远程服务器（公网）
    RGLinkModeLAN = 1,        // 局域网 Wi‑Fi（电脑接收端）
};

@interface RGPairing : NSObject
@property (nonatomic, readonly) RGLinkMode mode;
@property (nonatomic, readonly) BOOL hasMode;                 // NO when only a Key was scanned
@property (nonatomic, copy, readonly) NSArray<NSString *> *addresses;
@property (nonatomic, copy, readonly) NSString *key;
@property (nonatomic, copy, readonly, nullable) NSString *name;

/// nil + error when the text is not an AI魔颜 pairing code.
+ (nullable instancetype)pairingFromString:(NSString *)text error:(NSError **)error;
/// Addresses as shown in the address field ("a, b").
- (NSString *)addressText;
@end

/// Splits "a, b；c" into trimmed, non-empty addresses.
NSArray<NSString *> *RGSplitAddresses(NSString *text);
/// YES for 10/8, 172.16/12, 192.168/16, 169.254/16, IPv6 fc00::/7 + fe80::/10, and *.local names.
BOOL RGIsLocalHost(NSString *host);

NS_ASSUME_NONNULL_END
