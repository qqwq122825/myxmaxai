//
//  RGConnection.h
//  AIMoYan
//
//  Client for the remote-gpu server ("direct-1" protocol, same as RemoteGpuLocal.exe):
//  TLS pinned to the certificate fingerprint inside the RGK1 Key, bearer-key auth,
//  one WebSocket carrying H.264 "RGV1" frames both ways plus JSON control messages.
//

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Server address + Key, parsed and validated.
@interface RGServerInfo : NSObject
@property (nonatomic, copy, readonly) NSString *host;
@property (nonatomic, readonly) NSInteger port;
@property (nonatomic, copy, readonly) NSString *secret;        // bearer key
@property (nonatomic, copy, readonly) NSData *fingerprint;     // SHA-256 of the server certificate (32 bytes)
/// address: "1.2.3.4", "1.2.3.4:8766", "https://host:port", "[::1]:8766". key: "RGK1.xxxx".
+ (nullable instancetype)infoWithAddress:(NSString *)address key:(NSString *)key error:(NSError **)error;
@end

/// Negotiated video settings (must match the encoder output exactly).
@interface RGVideoSettings : NSObject
@property (nonatomic) int width;
@property (nonatomic) int height;
@property (nonatomic) int fps;
@property (nonatomic) int bitrateKbps;
- (NSDictionary *)dictionary;
@end

@class RGConnection;

@protocol RGConnectionDelegate <NSObject>
/// Connected and the server accepted the video settings. Start sending frames.
- (void)connectionDidBecomeReady:(RGConnection *)connection;
/// The server started / stopped returning the processed picture.
- (void)connection:(RGConnection *)connection returnActiveChanged:(BOOL)active;
/// One returned "RGV1" message (header + H.264 Annex-B).
- (void)connection:(RGConnection *)connection didReceiveVideo:(NSData *)message;
/// Connection ended. error is nil when the user disconnected.
- (void)connection:(RGConnection *)connection didCloseWithError:(nullable NSError *)error;
@end

@interface RGConnection : NSObject
@property (nonatomic, weak, nullable) id<RGConnectionDelegate> delegate;   // called on the main queue
@property (nonatomic, readonly) BOOL ready;
@property (nonatomic, readonly) BOOL returnActive;
/// Round-trip time in ms, or -1 when not measured yet.
@property (atomic, readonly) double rttMs;
@property (atomic, readonly) uint64_t sentFrames, sentBytes, receivedFrames, receivedBytes, droppedFrames;

- (instancetype)initWithServer:(RGServerInfo *)server settings:(RGVideoSettings *)settings;
- (void)connect;
- (void)disconnect;
/// False when the network is backed up; drop the camera frame instead of queueing (keeps latency low).
- (BOOL)canSendVideo;
- (void)noteDroppedFrame;
- (void)sendVideoMessage:(NSData *)message;
@end

NS_ASSUME_NONNULL_END
