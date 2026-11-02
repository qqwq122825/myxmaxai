//
//  ViewController.m
//  AIMoYan
//
//  One screen: camera preview, address + Key, connect. Two link modes:
//    远程服务器 — push to the rented GPU server over the internet;
//    局域网     — push to the PC receiver on the same Wi‑Fi (scan the QR code it shows).
//  Tap = focus (then drag up/down = brightness), long-press = AE/AF lock, double-tap = switch camera.
//

#import "ViewController.h"
#import <AVFoundation/AVFoundation.h>
#import <Security/Security.h>
#import "RGCamera.h"
#import "RGConnection.h"
#import "RGEncoder.h"
#import "RGPairing.h"
#import "RGReturnView.h"
#import "RGScanViewController.h"
#import <Network/Network.h>

#pragma mark - Palette (same as the AI魔颜 desktop app)

static UIColor *RGColor(uint32_t hex, CGFloat alpha) {
    return [UIColor colorWithRed:((hex >> 16) & 0xFF) / 255.0 green:((hex >> 8) & 0xFF) / 255.0
                            blue:(hex & 0xFF) / 255.0 alpha:alpha];
}
#define RG_INK      RGColor(0x1A1420, 1)
#define RG_PANEL    RGColor(0x231B2A, 0.92)
#define RG_RAISED   RGColor(0x2D2436, 1)
#define RG_LINE     RGColor(0x3B3046, 1)
#define RG_TEXT     RGColor(0xF4EDF6, 1)
#define RG_MUTED    RGColor(0xA797B1, 1)
#define RG_RING     RGColor(0xFFD6B0, 1)
#define RG_LIVE     RGColor(0xFF4F86, 1)
#define RG_OK       RGColor(0x5BD6A8, 1)

#pragma mark - Keychain (the Key is a credential, keep it out of UserDefaults)

static NSString *const kKeychainService = @"AIMoYan.server";
static NSString *const kAddressDefaultsKey = @"AIMoYan.serverAddress";
static NSString *const kLANAddressDefaultsKey = @"AIMoYan.lanAddress";
static NSString *const kModeDefaultsKey = @"AIMoYan.linkMode";
static NSString *const kQualityDefaultsKey = @"AIMoYan.quality";
static NSString *const kPortraitDefaultsKey = @"AIMoYan.portrait";

static NSString *RGKeychainAccount(RGLinkMode mode) { return mode == RGLinkModeLAN ? @"lan-key" : @"key"; }

static void RGKeychainSave(RGLinkMode mode, NSString *value) {
    NSDictionary *query = @{(id)kSecClass: (id)kSecClassGenericPassword, (id)kSecAttrService: kKeychainService,
                            (id)kSecAttrAccount: RGKeychainAccount(mode)};
    SecItemDelete((__bridge CFDictionaryRef)query);
    if (value.length == 0) return;
    NSMutableDictionary *item = [query mutableCopy];
    item[(id)kSecValueData] = [value dataUsingEncoding:NSUTF8StringEncoding];
    item[(id)kSecAttrAccessible] = (id)kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly;
    SecItemAdd((__bridge CFDictionaryRef)item, NULL);
}

static NSString *RGKeychainLoad(RGLinkMode mode) {
    NSDictionary *query = @{(id)kSecClass: (id)kSecClassGenericPassword, (id)kSecAttrService: kKeychainService,
                            (id)kSecAttrAccount: RGKeychainAccount(mode), (id)kSecReturnData: @YES,
                            (id)kSecMatchLimit: (id)kSecMatchLimitOne};
    CFTypeRef result = NULL;
    if (SecItemCopyMatching((__bridge CFDictionaryRef)query, &result) != errSecSuccess || !result) return @"";
    NSData *data = CFBridgingRelease(result);
    return [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] ?: @"";
}

#pragma mark - Small views

/// UIView whose backing layer is the camera preview.
@interface RGPreviewView : UIView
@property (nonatomic, readonly) AVCaptureVideoPreviewLayer *previewLayer;
@end
@implementation RGPreviewView
+ (Class)layerClass { return AVCaptureVideoPreviewLayer.class; }
- (AVCaptureVideoPreviewLayer *)previewLayer { return (AVCaptureVideoPreviewLayer *)self.layer; }
@end

typedef NS_ENUM(NSInteger, RGState) { RGStateIdle, RGStateConnecting, RGStateLive };

@interface ViewController () <RGConnectionDelegate, UITextFieldDelegate, UIGestureRecognizerDelegate>
// Media
@property (nonatomic, strong) RGCamera *camera;
@property (atomic, strong, nullable) RGConnection *connection;
@property (atomic, strong, nullable) RGEncoder *encoder;
// Views
@property (nonatomic, strong) RGPreviewView *previewView;
@property (nonatomic, strong) RGReturnView *returnView;
@property (nonatomic, strong) UIView *focusSquare;
@property (nonatomic, strong) UIView *exposureTrack;        // vertical line next to the focus box
@property (nonatomic, strong) UIImageView *exposureSun;     // ☀ knob, moves with the brightness
@property (nonatomic, strong) UILabel *lockLabel;           // "AE/AF 锁定"
@property (nonatomic) float exposureBias;
@property (nonatomic) float panStartBias;
@property (nonatomic, strong, nullable) NSTimer *focusHideTimer;
@property (nonatomic, strong) UIView *statusPill;
@property (nonatomic, strong) UIView *statusDot;
@property (nonatomic, strong) UILabel *statusLabel;
@property (nonatomic, strong) UILabel *statsLabel;
@property (nonatomic, strong) UIButton *switchButton;
@property (nonatomic, strong) UIButton *qualityButton;
@property (nonatomic, strong) UIButton *orientationButton;
@property (nonatomic, strong) UIView *panel;
@property (nonatomic, strong) UIStackView *formStack;
@property (nonatomic, strong) UISegmentedControl *modeControl;
@property (nonatomic, strong) UILabel *addressCaption;
@property (nonatomic, strong) UILabel *keyCaption;
@property (nonatomic, strong) UIButton *scanButton;
@property (nonatomic, strong) UITextField *addressField;
@property (nonatomic, strong) UITextField *keyField;
@property (nonatomic, strong) UIButton *connectButton;
@property (nonatomic, strong) UILabel *hintLabel;
// State
@property (nonatomic) RGState state;
@property (nonatomic) RGLinkMode linkMode;
@property (nonatomic, strong) NSMutableArray<RGServerInfo *> *pendingServers;   // other addresses still to try
@property (nonatomic, strong, nullable) nw_path_monitor_t pathMonitor;
@property (atomic) BOOL onWiFi;
@property (nonatomic, strong, nullable) RGPairing *pendingPairing;              // opened via link before the view loaded
@property (nonatomic) RGQuality quality;
@property (nonatomic) BOOL portrait;
@property (nonatomic) BOOL returnActive;
@property (nonatomic) BOOL localIsBig;           // user swapped the windows
@property (nonatomic, strong, nullable) NSTimer *statsTimer;
@property (nonatomic) uint64_t lastSent, lastSentBytes, lastReceived;
@end

@implementation ViewController

- (UIStatusBarStyle)preferredStatusBarStyle { return UIStatusBarStyleLightContent; }
- (UIInterfaceOrientationMask)supportedInterfaceOrientations { return UIInterfaceOrientationMaskPortrait; }

- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = RG_INK;
    self.overrideUserInterfaceStyle = UIUserInterfaceStyleDark;

    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    self.quality = [defaults integerForKey:kQualityDefaultsKey] == RGQuality1080p ? RGQuality1080p : RGQuality720p;
    self.portrait = [defaults objectForKey:kPortraitDefaultsKey] ? [defaults boolForKey:kPortraitDefaultsKey] : YES;
    self.linkMode = [defaults integerForKey:kModeDefaultsKey] == RGLinkModeLAN ? RGLinkModeLAN : RGLinkModeRemote;
    self.pendingServers = [NSMutableArray array];
    self.onWiFi = YES;

    [self buildViews];
    [self buildGestures];
    [self loadFieldsForMode];
    [self applyState:RGStateIdle];
    [self refreshOptionButtons];
    [self startWiFiMonitor];

    self.camera = [RGCamera new];
    self.previewView.previewLayer.session = self.camera.session;
    self.camera.previewLayer = self.previewView.previewLayer;
    __weak typeof(self) weakSelf = self;
    self.camera.onFrame = ^(CVPixelBufferRef pixelBuffer) { [weakSelf handleCameraFrame:pixelBuffer]; };
    [self startCamera];

    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(appDidEnterBackground)
                                               name:UIApplicationDidEnterBackgroundNotification object:nil];

    if (self.pendingPairing) {
        RGPairing *pairing = self.pendingPairing;
        self.pendingPairing = nil;
        dispatch_async(dispatch_get_main_queue(), ^{ [self applyPairing:pairing]; });
    }
}

- (void)dealloc {
    [NSNotificationCenter.defaultCenter removeObserver:self];
    if (_pathMonitor) nw_path_monitor_cancel(_pathMonitor);
}

#pragma mark Link mode (远程服务器 / 局域网)

- (NSString *)addressDefaultsKeyForMode:(RGLinkMode)mode {
    return mode == RGLinkModeLAN ? kLANAddressDefaultsKey : kAddressDefaultsKey;
}

- (void)loadFieldsForMode {
    self.modeControl.selectedSegmentIndex = self.linkMode;
    self.addressField.text = [NSUserDefaults.standardUserDefaults stringForKey:[self addressDefaultsKeyForMode:self.linkMode]];
    self.keyField.text = RGKeychainLoad(self.linkMode);
    BOOL lan = self.linkMode == RGLinkModeLAN;
    self.addressCaption.text = lan ? @"电脑地址（扫码自动填写）" : @"服务器地址";
    self.keyCaption.text = lan ? @"连接 Key（扫码自动填写）" : @"连接 Key";
    self.addressField.attributedPlaceholder = [[NSAttributedString alloc]
        initWithString:lan ? @"例如 192.168.1.23:8766" : @"服务器地址，例如 1.2.3.4:8766"
            attributes:@{NSForegroundColorAttributeName: RG_MUTED}];
    self.hintLabel.text = lan ? @"手机和电脑连同一个 Wi‑Fi · 电脑打开接收端，点地址栏右边的扫码按钮"
                              : @"轻点对焦后上下滑调亮度 · 长按锁定对焦和亮度 · 双击切换前后摄像头";
    [self refreshOptionButtons];
}

- (void)saveFieldsForMode {
    [NSUserDefaults.standardUserDefaults setObject:self.addressField.text ?: @"" forKey:[self addressDefaultsKeyForMode:self.linkMode]];
    RGKeychainSave(self.linkMode, self.keyField.text ?: @"");
}

- (void)setLinkModeOption:(RGLinkMode)mode {
    if (mode == self.linkMode) return;
    [self saveFieldsForMode];
    self.linkMode = mode;
    [NSUserDefaults.standardUserDefaults setInteger:mode forKey:kModeDefaultsKey];
    [self loadFieldsForMode];
    [self setStatus:mode == RGLinkModeLAN ? @"局域网模式 · 未连接" : @"AI魔颜 · 未连接" color:RG_MUTED];
}

- (void)modeChanged:(UISegmentedControl *)control {
    [self.view endEditing:YES];
    [self setLinkModeOption:control.selectedSegmentIndex == 1 ? RGLinkModeLAN : RGLinkModeRemote];
}

/// Tracks whether the phone is on Wi‑Fi (LAN mode needs it).
- (void)startWiFiMonitor {
    nw_path_monitor_t monitor = nw_path_monitor_create();
    nw_path_monitor_set_queue(monitor, dispatch_get_main_queue());
    __weak typeof(self) weakSelf = self;
    nw_path_monitor_set_update_handler(monitor, ^(nw_path_t path) {
        weakSelf.onWiFi = nw_path_uses_interface_type(path, nw_interface_type_wifi) ||
                          nw_path_uses_interface_type(path, nw_interface_type_wired);
    });
    nw_path_monitor_start(monitor);
    self.pathMonitor = monitor;
}

#pragma mark QR code

- (void)scanTapped {
    [self.view endEditing:YES];
    if (self.state != RGStateIdle) { [self showToast:@"请先断开再扫码"]; return; }
    if ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo] == AVAuthorizationStatusNotDetermined) {
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL granted) {
            dispatch_async(dispatch_get_main_queue(), ^{ [self scanTapped]; });
        }];
        return;
    }
    [self.camera stop];                        // one capture session at a time
    RGScanViewController *scanner = [RGScanViewController new];
    scanner.modalPresentationStyle = UIModalPresentationFullScreen;
    __weak typeof(self) weakSelf = self;
    scanner.completion = ^(NSString *text) {
        if (weakSelf.camera.session.inputs.count) [weakSelf.camera resume];
        else [weakSelf startCamera];              // camera permission was granted only now
        if (text) [weakSelf handleScannedText:text];
    };
    [self presentViewController:scanner animated:YES completion:nil];
}

- (void)handleScannedText:(NSString *)text {
    NSError *error = nil;
    RGPairing *pairing = [RGPairing pairingFromString:text error:&error];
    if (!pairing) { [self showAlert:@"无法识别" message:error.localizedDescription]; return; }
    [self applyPairing:pairing];
}

/// Opened from an aimoyan:// link (e.g. scanned with the system Camera app).
- (void)handlePairingURL:(NSURL *)url {
    NSError *error = nil;
    RGPairing *pairing = [RGPairing pairingFromString:url.absoluteString error:&error];
    if (!pairing) {
        if (self.isViewLoaded) [self showAlert:@"无法识别" message:error.localizedDescription];
        return;
    }
    if (!self.isViewLoaded) { self.pendingPairing = pairing; return; }
    [self applyPairing:pairing];
}

- (void)applyPairing:(RGPairing *)pairing {
    if (self.state != RGStateIdle) { [self showToast:@"请先断开再更换连接"]; return; }
    if (!pairing.hasMode) {                       // only a Key
        self.keyField.text = pairing.key;
        RGKeychainSave(self.linkMode, pairing.key);
        [self showToast:@"已填入 Key"];
        return;
    }
    [self setLinkModeOption:pairing.mode];
    self.addressField.text = pairing.addressText;
    self.keyField.text = pairing.key;
    [self saveFieldsForMode];
    NSString *who = pairing.name.length ? pairing.name : (pairing.mode == RGLinkModeLAN ? @"电脑" : @"服务器");
    [self showToast:[NSString stringWithFormat:@"已识别：%@", who]];
    [self connectTapped];                         // scanning means "connect to this"
}

#pragma mark Camera

- (void)startCamera {
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    if (status == AVAuthorizationStatusNotDetermined) {
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL granted) {
            dispatch_async(dispatch_get_main_queue(), ^{ [self startCamera]; });
        }];
        return;
    }
    if (status != AVAuthorizationStatusAuthorized) {
        [self setStatus:@"没有相机权限" color:RG_LIVE];
        UIAlertController *alert = [UIAlertController alertControllerWithTitle:@"需要相机权限"
            message:@"请在 设置 → AI魔颜 中打开相机权限。" preferredStyle:UIAlertControllerStyleAlert];
        [alert addAction:[UIAlertAction actionWithTitle:@"去设置" style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) {
            [UIApplication.sharedApplication openURL:[NSURL URLWithString:UIApplicationOpenSettingsURLString] options:@{} completionHandler:nil];
        }]];
        [alert addAction:[UIAlertAction actionWithTitle:@"取消" style:UIAlertActionStyleCancel handler:nil]];
        [self presentViewController:alert animated:YES completion:nil];
        return;
    }
    [self.camera startWithPosition:AVCaptureDevicePositionBack quality:self.quality portrait:self.portrait completion:^(NSError *error) {
        if (error) [self setStatus:error.localizedDescription color:RG_LIVE];
    }];
}

/// Capture queue. Drop frames rather than queue them when the uplink is busy (keeps delay low).
- (void)handleCameraFrame:(CVPixelBufferRef)pixelBuffer {
    RGConnection *connection = self.connection;
    RGEncoder *encoder = self.encoder;
    if (!connection || !encoder || !connection.ready) return;
    if (![connection canSendVideo]) { [connection noteDroppedFrame]; return; }
    [encoder encodePixelBuffer:pixelBuffer];
}

#pragma mark Connect

- (void)connectTapped {
    [self.view endEditing:YES];
    if (self.state != RGStateIdle) { [self disconnectWithMessage:@"已断开"]; return; }

    BOOL lan = self.linkMode == RGLinkModeLAN;
    NSArray<NSString *> *addresses = RGSplitAddresses(self.addressField.text ?: @"");
    if (addresses.count == 0) {
        [self showError:lan ? @"请先扫描电脑接收端显示的二维码，或手动填写电脑地址。" : @"请填写服务器地址，例如 1.2.3.4:8766。"];
        return;
    }
    NSMutableArray<RGServerInfo *> *servers = [NSMutableArray array];
    for (NSString *address in addresses) {
        NSError *error = nil;
        RGServerInfo *server = [RGServerInfo infoWithAddress:address key:self.keyField.text ?: @"" error:&error];
        if (!server) { [self showError:error.localizedDescription]; return; }
        [servers addObject:server];
    }
    if (lan && !self.onWiFi) {
        [self showError:@"手机现在没有连接 Wi‑Fi。局域网模式需要手机和电脑连同一个 Wi‑Fi（路由器）。"];
        return;
    }
    [self saveFieldsForMode];
    [self.pendingServers setArray:servers];
    [self applyState:RGStateConnecting];
    [self connectToNextServer];
}

- (int)bitrateKbps {
    // Wi‑Fi has bandwidth to spare: higher bitrate = cleaner picture for the face swap.
    if (self.linkMode == RGLinkModeLAN) return self.quality == RGQuality1080p ? 12000 : 6000;
    return self.quality == RGQuality1080p ? 8000 : 4000;
}

/// Tries the addresses in order (a PC can have several network cards); the first one that answers wins.
- (void)connectToNextServer {
    RGServerInfo *server = self.pendingServers.firstObject;
    if (!server) return;
    [self.pendingServers removeObjectAtIndex:0];

    CGSize size = [RGCamera frameSizeForQuality:self.quality portrait:self.portrait];
    RGVideoSettings *settings = [RGVideoSettings new];
    settings.width = (int)size.width;
    settings.height = (int)size.height;
    settings.fps = 30;
    settings.bitrateKbps = [self bitrateKbps];

    RGConnection *connection = [[RGConnection alloc] initWithServer:server settings:settings];
    connection.localNetwork = self.linkMode == RGLinkModeLAN;
    connection.delegate = self;
    self.connection = connection;
    NSString *target = self.linkMode == RGLinkModeLAN ? @"电脑" : @"";
    [self setStatus:[NSString stringWithFormat:@"正在连接%@ %@:%ld …", target, server.host, (long)server.port] color:RG_RING];
    [connection connect];
}

- (void)disconnectWithMessage:(NSString *)message {
    [self.pendingServers removeAllObjects];
    RGConnection *connection = self.connection;
    self.connection = nil;
    connection.delegate = nil;
    [connection disconnect];
    [self.encoder invalidate];
    self.encoder = nil;
    [self.returnView reset];
    self.returnActive = NO;
    [self applyState:RGStateIdle];
    [self setStatus:message color:RG_MUTED];
    [self layoutVideoAnimated:YES];
}

- (void)appDidEnterBackground {
    // iOS stops the camera in the background; end the session cleanly instead of letting it time out.
    if (self.state != RGStateIdle) [self disconnectWithMessage:@"已切到后台，连接已断开"];
}

#pragma mark RGConnectionDelegate

- (void)connectionDidBecomeReady:(RGConnection *)connection {
    if (connection != self.connection) return;
    CGSize size = [RGCamera frameSizeForQuality:self.quality portrait:self.portrait];
    RGEncoder *encoder = [[RGEncoder alloc] initWithWidth:(int)size.width height:(int)size.height
                                                      fps:30 bitrateKbps:[self bitrateKbps]];
    if (!encoder) { [self disconnectWithMessage:@"这台手机无法启动 H.264 编码"]; return; }
    __weak RGConnection *weakConnection = connection;
    encoder.onMessage = ^(NSData *message) { [weakConnection sendVideoMessage:message]; };
    self.encoder = encoder;
    self.lastSent = self.lastSentBytes = self.lastReceived = 0;
    [self applyState:RGStateLive];
    [self.pendingServers removeAllObjects];
    [self setStatus:self.linkMode == RGLinkModeLAN ? @"局域网推流中 · 电脑接收端已连接" : @"推流中 · 换脸结果在电脑端接收"
              color:RG_OK];
}

- (void)connection:(RGConnection *)connection returnActiveChanged:(BOOL)active {
    // Push-only: the phone never shows the processed picture (it goes to the PC).
    if (connection != self.connection || !active) return;
    // Each resumed return stream starts with a new keyframe.
    [self.returnView reset];
    self.returnActive = active;
    self.localIsBig = NO;
    [self setStatus:active ? @"已连接 · 正在接收处理后的画面" : @"已连接 · 等待服务器开始回传" color:RG_OK];
    [self layoutVideoAnimated:YES];
}

- (void)connection:(RGConnection *)connection didReceiveVideo:(NSData *)message {
    if (connection != self.connection) return;
    [self.returnView enqueueMessage:message];
}

- (void)connection:(RGConnection *)connection didCloseWithError:(NSError *)error {
    if (connection != self.connection) return;
    // Address did not answer: try the next one from the QR code before giving up.
    if (error.code == RGConnectionErrorUnreachable && [error.domain isEqual:RGConnectionErrorDomain] &&
        self.pendingServers.count > 0 && self.state == RGStateConnecting) {
        connection.delegate = nil;
        [self connectToNextServer];
        return;
    }
    [self disconnectWithMessage:error ? @"连接已断开" : @"已断开"];
    if (error) [self showError:error.localizedDescription];
}

#pragma mark Stats

- (void)tickStats {
    RGConnection *c = self.connection;
    if (!c || self.state != RGStateLive) { self.statsLabel.text = @""; return; }
    uint64_t sent = c.sentFrames, bytes = c.sentBytes, received = c.receivedFrames;
    double upFps = sent - self.lastSent, mbps = (bytes - self.lastSentBytes) * 8.0 / 1e6, downFps = received - self.lastReceived;
    self.lastSent = sent; self.lastSentBytes = bytes; self.lastReceived = received;
    NSString *rtt = c.rttMs >= 0 ? [NSString stringWithFormat:@"%.0f ms", c.rttMs] : @"—";
    CGSize size = [RGCamera frameSizeForQuality:self.quality portrait:self.portrait];
    (void)downFps;
    self.statsLabel.text = [NSString stringWithFormat:@"%.0f×%.0f  推流 %.0f fps  %.1f Mbps   网络延迟 %@",
                            size.width, size.height, upFps, mbps, rtt];
}

#pragma mark UI state

- (void)applyState:(RGState)state {
    _state = state;
    BOOL idle = state == RGStateIdle;
    self.addressField.enabled = self.keyField.enabled = idle;
    self.modeControl.enabled = self.scanButton.enabled = idle;
    self.qualityButton.enabled = self.orientationButton.enabled = idle;
    self.qualityButton.alpha = self.orientationButton.alpha = idle ? 1 : 0.4;

    NSString *title = state == RGStateIdle ? @"连接" : state == RGStateConnecting ? @"连接中…（点击取消）" : @"断开";
    UIColor *color = state == RGStateLive ? RG_RAISED : RG_LIVE;
    UIButtonConfiguration *config = self.connectButton.configuration;
    config.title = title;
    config.baseBackgroundColor = color;
    UIBackgroundConfiguration *background = config.background;
    background.strokeColor = state == RGStateLive ? RG_LIVE : UIColor.clearColor;
    background.strokeWidth = state == RGStateLive ? 1 : 0;
    config.background = background;
    self.connectButton.configuration = config;

    [UIView animateWithDuration:0.25 animations:^{
        self.formStack.hidden = !idle;
        self.formStack.alpha = idle ? 1 : 0;
        self.hintLabel.hidden = !idle;
        self.modeControl.hidden = !idle;
        self.modeControl.alpha = idle ? 1 : 0;
    }];
    UIApplication.sharedApplication.idleTimerDisabled = !idle;   // keep the screen on while streaming

    [self.statsTimer invalidate];
    self.statsTimer = nil;
    if (state == RGStateLive) {
        __weak typeof(self) weakSelf = self;
        self.statsTimer = [NSTimer scheduledTimerWithTimeInterval:1 repeats:YES block:^(NSTimer *t) { [weakSelf tickStats]; }];
    } else {
        self.statsLabel.text = @"";
    }
}

- (void)setStatus:(NSString *)text color:(UIColor *)color {
    self.statusLabel.text = text;
    self.statusDot.backgroundColor = color;
}

- (void)showError:(NSString *)message {
    [self showAlert:@"连接失败" message:message];
}

- (void)showAlert:(NSString *)title message:(NSString *)message {
    UIAlertController *alert = [UIAlertController alertControllerWithTitle:title message:message
                                                            preferredStyle:UIAlertControllerStyleAlert];
    [alert addAction:[UIAlertAction actionWithTitle:@"好" style:UIAlertActionStyleDefault handler:nil]];
    if (!self.presentedViewController) [self presentViewController:alert animated:YES completion:nil];
}

- (void)showToast:(NSString *)text {
    UILabel *toast = [UILabel new];
    toast.text = [NSString stringWithFormat:@"  %@  ", text];
    toast.textColor = RG_INK;
    toast.backgroundColor = RG_TEXT;
    toast.font = [UIFont systemFontOfSize:14 weight:UIFontWeightMedium];
    toast.layer.cornerRadius = 16;
    toast.layer.masksToBounds = YES;
    toast.textAlignment = NSTextAlignmentCenter;
    [toast sizeToFit];
    toast.frame = CGRectMake(0, 0, toast.bounds.size.width + 12, 32);
    toast.center = CGPointMake(CGRectGetMidX(self.view.bounds), CGRectGetMidY(self.view.bounds));
    toast.alpha = 0;
    [self.view addSubview:toast];
    [UIView animateWithDuration:0.2 animations:^{ toast.alpha = 1; } completion:^(BOOL f) {
        [UIView animateWithDuration:0.3 delay:1.2 options:0 animations:^{ toast.alpha = 0; } completion:^(BOOL g) { [toast removeFromSuperview]; }];
    }];
}

#pragma mark Options (camera / resolution / orientation)

- (void)switchCameraTapped {
    [self.camera switchCamera];
    [self setLocked:NO];
    self.exposureBias = 0;
    [self showToast:self.camera.position == AVCaptureDevicePositionBack ? @"前置摄像头" : @"后置摄像头"];
}

- (void)setQualityOption:(RGQuality)quality portrait:(BOOL)portrait {
    if (self.state != RGStateIdle) { [self showToast:@"请先断开再修改"]; return; }
    self.quality = quality;
    self.portrait = portrait;
    [NSUserDefaults.standardUserDefaults setInteger:quality forKey:kQualityDefaultsKey];
    [NSUserDefaults.standardUserDefaults setBool:portrait forKey:kPortraitDefaultsKey];
    [self.camera setQuality:quality portrait:portrait];
    [self refreshOptionButtons];
    CGSize size = [RGCamera frameSizeForQuality:quality portrait:portrait];
    [self showToast:[NSString stringWithFormat:@"%.0f × %.0f", size.width, size.height]];
}

- (void)refreshOptionButtons {
    __weak typeof(self) weakSelf = self;
    NSArray *qualities = @[@[@"720p", @(RGQuality720p)], @[self.linkMode == RGLinkModeLAN ? @"1080p（局域网推荐）" : @"1080p（服务器需支持）", @(RGQuality1080p)]];
    NSMutableArray *qualityActions = [NSMutableArray array];
    for (NSArray *q in qualities) {
        RGQuality value = [q[1] integerValue];
        UIAction *action = [UIAction actionWithTitle:q[0] image:nil identifier:nil handler:^(UIAction *a) {
            [weakSelf setQualityOption:value portrait:weakSelf.portrait];
        }];
        action.state = value == self.quality ? UIMenuElementStateOn : UIMenuElementStateOff;
        [qualityActions addObject:action];
    }
    self.qualityButton.menu = [UIMenu menuWithTitle:@"分辨率" children:qualityActions];
    [self setLabel:self.quality == RGQuality1080p ? @"1080" : @"720" onButton:self.qualityButton];

    UIAction *portraitAction = [UIAction actionWithTitle:@"竖屏" image:[UIImage systemImageNamed:@"rectangle.portrait"] identifier:nil
        handler:^(UIAction *a) { [weakSelf setQualityOption:weakSelf.quality portrait:YES]; }];
    UIAction *landscapeAction = [UIAction actionWithTitle:@"横屏（手机横着拿）" image:[UIImage systemImageNamed:@"rectangle"] identifier:nil
        handler:^(UIAction *a) { [weakSelf setQualityOption:weakSelf.quality portrait:NO]; }];
    portraitAction.state = self.portrait ? UIMenuElementStateOn : UIMenuElementStateOff;
    landscapeAction.state = self.portrait ? UIMenuElementStateOff : UIMenuElementStateOn;
    self.orientationButton.menu = [UIMenu menuWithTitle:@"画面方向" children:@[portraitAction, landscapeAction]];
    [self setLabel:self.portrait ? @"竖屏" : @"横屏" onButton:self.orientationButton];
}

- (void)setLabel:(NSString *)label onButton:(UIButton *)button {
    UIButtonConfiguration *config = button.configuration;
    NSDictionary *attrs = @{NSFontAttributeName: [UIFont systemFontOfSize:11 weight:UIFontWeightSemibold]};
    config.attributedTitle = [[NSAttributedString alloc] initWithString:label attributes:attrs];
    button.configuration = config;
}

#pragma mark Gestures: tap = focus, drag = brightness, long-press = lock, double-tap = switch camera

- (void)buildGestures {
    UITapGestureRecognizer *doubleTap = [[UITapGestureRecognizer alloc] initWithTarget:self action:@selector(handleDoubleTap:)];
    doubleTap.numberOfTapsRequired = 2;
    UITapGestureRecognizer *tap = [[UITapGestureRecognizer alloc] initWithTarget:self action:@selector(handleTap:)];
    [tap requireGestureRecognizerToFail:doubleTap];
    UILongPressGestureRecognizer *longPress = [[UILongPressGestureRecognizer alloc] initWithTarget:self action:@selector(handleLongPress:)];
    longPress.minimumPressDuration = 0.5;
    UIPanGestureRecognizer *pan = [[UIPanGestureRecognizer alloc] initWithTarget:self action:@selector(handleExposurePan:)];
    pan.maximumNumberOfTouches = 1;
    for (UIGestureRecognizer *g in @[doubleTap, tap, longPress, pan]) {
        g.delegate = self;
        [self.view addGestureRecognizer:g];
    }
}

- (BOOL)gestureRecognizerShouldBegin:(UIGestureRecognizer *)gesture {
    // Brightness dragging only while the focus box is showing (right after a tap / long-press).
    if ([gesture isKindOfClass:UIPanGestureRecognizer.class]) return self.focusSquare.alpha > 0.01 && self.isLocalBig;
    if ([gesture isKindOfClass:UILongPressGestureRecognizer.class]) return self.isLocalBig;
    return YES;
}

- (BOOL)gestureRecognizer:(UIGestureRecognizer *)gesture shouldReceiveTouch:(UITouch *)touch {
    // Ignore touches on the panel and buttons.
    UIView *v = touch.view;
    while (v && v != self.view) {
        if (v == self.panel || [v isKindOfClass:UIControl.class] || v == self.statusPill) return NO;
        v = v.superview;
    }
    return YES;
}

- (BOOL)isLocalBig {
    return !self.returnActive || self.localIsBig;
}

- (void)handleTap:(UITapGestureRecognizer *)tap {
    if (self.addressField.isFirstResponder || self.keyField.isFirstResponder) {
        [self.view endEditing:YES];
        return;
    }
    CGPoint point = [tap locationInView:self.view];
    UIView *small = self.isLocalBig ? self.returnView : self.previewView;
    if (self.returnActive && CGRectContainsPoint(small.frame, point)) {   // tap the small window: swap
        self.localIsBig = !self.localIsBig;
        [self layoutVideoAnimated:YES];
        return;
    }
    if (!self.isLocalBig) return;                                         // focus only on the camera picture
    CGPoint inPreview = [self.view convertPoint:point toView:self.previewView];
    CGPoint devicePoint = [self.previewView.previewLayer captureDevicePointOfInterestForPoint:inPreview];
    [self.camera focusAtDevicePoint:devicePoint];
    self.exposureBias = 0;
    [self setLocked:NO];
    [self showFocusAt:point];
}

- (void)handleLongPress:(UILongPressGestureRecognizer *)press {
    if (press.state != UIGestureRecognizerStateBegan) return;
    CGPoint point = [press locationInView:self.view];
    CGPoint inPreview = [self.view convertPoint:point toView:self.previewView];
    [self.camera lockFocusAndExposureAtDevicePoint:[self.previewView.previewLayer captureDevicePointOfInterestForPoint:inPreview]];
    self.exposureBias = 0;
    [self showFocusAt:point];
    [self setLocked:YES];
    [[[UIImpactFeedbackGenerator alloc] initWithStyle:UIImpactFeedbackStyleMedium] impactOccurred];
}

- (void)setLocked:(BOOL)locked {
    [UIView animateWithDuration:0.2 animations:^{ self.lockLabel.alpha = locked ? 1 : 0; }];
}

/// Drag up = brighter, down = darker (≈ 1 EV per 150 pt), like the iPhone Camera app.
- (void)handleExposurePan:(UIPanGestureRecognizer *)pan {
    if (pan.state == UIGestureRecognizerStateBegan) self.panStartBias = self.exposureBias;
    CGFloat dy = [pan translationInView:self.view].y;
    self.exposureBias = [self.camera setExposureBias:self.panStartBias - (float)(dy / 150.0)];
    [self layoutExposureKnob];
    [self keepFocusUIVisible];
}

- (void)layoutExposureKnob {
    CGRect track = self.exposureTrack.frame;
    CGFloat t = (2.0 - self.exposureBias) / 4.0;            // +2 EV at the top, -2 EV at the bottom
    self.exposureSun.center = CGPointMake(CGRectGetMidX(track), CGRectGetMinY(track) + t * CGRectGetHeight(track));
}

- (void)keepFocusUIVisible {
    [self.focusHideTimer invalidate];
    for (UIView *v in @[self.focusSquare, self.exposureTrack, self.exposureSun]) { [v.layer removeAllAnimations]; v.alpha = 1; }
    __weak typeof(self) weakSelf = self;
    self.focusHideTimer = [NSTimer scheduledTimerWithTimeInterval:2.0 repeats:NO block:^(NSTimer *t) {
        [UIView animateWithDuration:0.4 animations:^{
            weakSelf.focusSquare.alpha = 0; weakSelf.exposureTrack.alpha = 0; weakSelf.exposureSun.alpha = 0;
        }];
    }];
}

- (void)handleDoubleTap:(UITapGestureRecognizer *)tap {
    [self switchCameraTapped];
}

- (void)showFocusAt:(CGPoint)point {
    UIView *square = self.focusSquare;
    square.center = point;
    // Slider sits to the right of the box (left side if the box is near the right edge).
    CGFloat side = point.x + 64 < CGRectGetMaxX(self.view.bounds) - 20 ? 1 : -1;
    self.exposureTrack.frame = CGRectMake(point.x + side * 58 - 0.75, point.y - 70, 1.5, 140);
    [self layoutExposureKnob];
    [self keepFocusUIVisible];
    square.transform = CGAffineTransformMakeScale(1.4, 1.4);
    [UIView animateWithDuration:0.25 animations:^{ square.transform = CGAffineTransformIdentity; }];
}

#pragma mark Layout

- (void)viewDidLayoutSubviews {
    [super viewDidLayoutSubviews];
    [self layoutVideoAnimated:NO];
}

- (void)layoutVideoAnimated:(BOOL)animated {
    CGRect full = self.view.bounds;
    CGFloat pipWidth = 116;
    CGFloat pipHeight = self.portrait ? pipWidth * 16.0 / 9.0 : pipWidth * 9.0 / 16.0;
    CGFloat top = self.view.safeAreaInsets.top + 64;
    CGRect pip = CGRectMake(CGRectGetMaxX(full) - pipWidth - 14, top, pipWidth, pipHeight);

    void (^apply)(void) = ^{
        if (!self.returnActive) {
            self.previewView.frame = full;
            self.returnView.frame = pip;
            self.returnView.alpha = 0;
        } else if (self.localIsBig) {
            self.previewView.frame = full;
            self.returnView.frame = pip;
            self.returnView.alpha = 1;
        } else {
            self.returnView.frame = full;
            self.previewView.frame = pip;
            self.returnView.alpha = 1;
        }
        UIView *small = self.isLocalBig ? self.returnView : self.previewView;
        UIView *big = small == self.returnView ? self.previewView : self.returnView;
        small.layer.cornerRadius = 12;
        small.layer.borderWidth = 1.5;
        small.layer.borderColor = [UIColor colorWithWhite:1 alpha:0.75].CGColor;
        big.layer.cornerRadius = 0;
        big.layer.borderWidth = 0;
        [self.view insertSubview:big atIndex:0];
        [self.view insertSubview:small aboveSubview:big];
    };
    if (animated) [UIView animateWithDuration:0.3 animations:apply]; else apply();
}

#pragma mark Build views

- (UIButton *)roundButtonWithSymbol:(NSString *)symbol {
    UIButtonConfiguration *config = [UIButtonConfiguration filledButtonConfiguration];
    config.image = [UIImage systemImageNamed:symbol withConfiguration:
                    [UIImageSymbolConfiguration configurationWithPointSize:18 weight:UIImageSymbolWeightSemibold]];
    config.imagePlacement = NSDirectionalRectEdgeTop;
    config.imagePadding = 2;
    config.baseBackgroundColor = RGColor(0x1A1420, 0.6);
    config.baseForegroundColor = RG_TEXT;
    config.cornerStyle = UIButtonConfigurationCornerStyleLarge;
    config.contentInsets = NSDirectionalEdgeInsetsMake(8, 6, 8, 6);
    UIButton *button = [UIButton buttonWithConfiguration:config primaryAction:nil];
    button.translatesAutoresizingMaskIntoConstraints = NO;
    [button.widthAnchor constraintEqualToConstant:54].active = YES;
    [button.heightAnchor constraintEqualToConstant:54].active = YES;
    return button;
}

- (UITextField *)fieldWithPlaceholder:(NSString *)placeholder {
    UITextField *field = [UITextField new];
    field.attributedPlaceholder = [[NSAttributedString alloc] initWithString:placeholder attributes:@{NSForegroundColorAttributeName: RG_MUTED}];
    field.textColor = RG_TEXT;
    field.font = [UIFont systemFontOfSize:15];
    field.backgroundColor = RG_INK;
    field.layer.cornerRadius = 10;
    field.layer.borderColor = RG_LINE.CGColor;
    field.layer.borderWidth = 1;
    field.autocorrectionType = UITextAutocorrectionTypeNo;
    field.autocapitalizationType = UITextAutocapitalizationTypeNone;
    field.spellCheckingType = UITextSpellCheckingTypeNo;
    field.leftView = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 12, 1)];
    field.leftViewMode = UITextFieldViewModeAlways;
    field.clearButtonMode = UITextFieldViewModeWhileEditing;
    field.delegate = self;
    [field.heightAnchor constraintEqualToConstant:46].active = YES;
    return field;
}

- (UILabel *)captionWithText:(NSString *)text {
    UILabel *label = [UILabel new];
    label.text = text;
    label.textColor = RG_MUTED;
    label.font = [UIFont systemFontOfSize:12];
    return label;
}

- (void)buildViews {
    // Video
    self.previewView = [RGPreviewView new];
    self.previewView.previewLayer.videoGravity = AVLayerVideoGravityResizeAspectFill;
    self.previewView.backgroundColor = UIColor.blackColor;
    self.previewView.clipsToBounds = YES;
    self.returnView = [RGReturnView new];
    self.returnView.clipsToBounds = YES;
    [self.view addSubview:self.previewView];
    [self.view addSubview:self.returnView];

    self.focusSquare = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 76, 76)];
    self.focusSquare.layer.borderColor = RG_RING.CGColor;
    self.focusSquare.layer.borderWidth = 1.5;
    self.focusSquare.layer.cornerRadius = 6;
    self.focusSquare.alpha = 0;
    self.focusSquare.userInteractionEnabled = NO;
    [self.view addSubview:self.focusSquare];

    self.exposureTrack = [UIView new];
    self.exposureTrack.backgroundColor = [RG_RING colorWithAlphaComponent:0.7];
    self.exposureTrack.alpha = 0;
    self.exposureTrack.userInteractionEnabled = NO;
    [self.view addSubview:self.exposureTrack];
    self.exposureSun = [[UIImageView alloc] initWithImage:[UIImage systemImageNamed:@"sun.max.fill"
        withConfiguration:[UIImageSymbolConfiguration configurationWithPointSize:20 weight:UIImageSymbolWeightBold]]];
    self.exposureSun.tintColor = RG_RING;
    self.exposureSun.alpha = 0;
    self.exposureSun.userInteractionEnabled = NO;
    [self.view addSubview:self.exposureSun];

    self.lockLabel = [UILabel new];
    self.lockLabel.text = @"  AE/AF 锁定  ";
    self.lockLabel.font = [UIFont systemFontOfSize:13 weight:UIFontWeightSemibold];
    self.lockLabel.textColor = RG_INK;
    self.lockLabel.backgroundColor = RG_RING;
    self.lockLabel.layer.cornerRadius = 6;
    self.lockLabel.layer.masksToBounds = YES;
    self.lockLabel.alpha = 0;
    self.lockLabel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:self.lockLabel];


    // Status pill (top)
    self.statusPill = [UIView new];
    self.statusPill.backgroundColor = RGColor(0x1A1420, 0.65);
    self.statusPill.layer.cornerRadius = 16;
    self.statusPill.translatesAutoresizingMaskIntoConstraints = NO;
    self.statusDot = [UIView new];
    self.statusDot.layer.cornerRadius = 4;
    self.statusDot.translatesAutoresizingMaskIntoConstraints = NO;
    self.statusLabel = [UILabel new];
    self.statusLabel.textColor = RG_TEXT;
    self.statusLabel.font = [UIFont systemFontOfSize:13 weight:UIFontWeightMedium];
    self.statusLabel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.statusPill addSubview:self.statusDot];
    [self.statusPill addSubview:self.statusLabel];
    [self.view addSubview:self.statusPill];
    [self setStatus:@"AI魔颜 · 未连接" color:RG_MUTED];

    self.statsLabel = [UILabel new];
    self.statsLabel.textColor = RG_TEXT;
    self.statsLabel.font = [UIFont monospacedDigitSystemFontOfSize:11 weight:UIFontWeightRegular];
    self.statsLabel.shadowColor = [UIColor colorWithWhite:0 alpha:0.7];
    self.statsLabel.shadowOffset = CGSizeMake(0, 1);
    self.statsLabel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:self.statsLabel];

    // Side buttons
    self.switchButton = [self roundButtonWithSymbol:@"arrow.triangle.2.circlepath.camera"];
    [self.switchButton addTarget:self action:@selector(switchCameraTapped) forControlEvents:UIControlEventTouchUpInside];
    self.qualityButton = [self roundButtonWithSymbol:@"sparkles.tv"];
    self.qualityButton.showsMenuAsPrimaryAction = YES;
    self.orientationButton = [self roundButtonWithSymbol:@"rotate.right"];
    self.orientationButton.showsMenuAsPrimaryAction = YES;
    UIStackView *side = [[UIStackView alloc] initWithArrangedSubviews:@[self.switchButton, self.qualityButton, self.orientationButton]];
    side.axis = UILayoutConstraintAxisVertical;
    side.spacing = 12;
    side.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:side];

    // Bottom panel
    self.panel = [UIView new];
    self.panel.backgroundColor = RG_PANEL;
    self.panel.layer.cornerRadius = 22;
    self.panel.layer.borderColor = RG_LINE.CGColor;
    self.panel.layer.borderWidth = 1;
    self.panel.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:self.panel];

    self.addressField = [self fieldWithPlaceholder:@"服务器地址，例如 1.2.3.4:8766"];
    self.addressField.keyboardType = UIKeyboardTypeURL;
    self.addressField.returnKeyType = UIReturnKeyNext;
    self.keyField = [self fieldWithPlaceholder:@"连接 Key（RGK1. 开头）"];
    self.keyField.secureTextEntry = YES;
    self.keyField.returnKeyType = UIReturnKeyGo;

    UIButtonConfiguration *pasteConfig = [UIButtonConfiguration tintedButtonConfiguration];
    pasteConfig.title = @"粘贴";
    pasteConfig.baseForegroundColor = RG_RING;
    pasteConfig.baseBackgroundColor = RG_RING;
    UIButton *paste = [UIButton buttonWithConfiguration:pasteConfig primaryAction:[UIAction actionWithHandler:^(UIAction *a) {
        NSString *text = UIPasteboard.generalPasteboard.string;
        if (text.length) self.keyField.text = [text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    }]];
    [paste.widthAnchor constraintEqualToConstant:64].active = YES;
    UIStackView *keyRow = [[UIStackView alloc] initWithArrangedSubviews:@[self.keyField, paste]];
    keyRow.spacing = 8;

    UIButtonConfiguration *scanConfig = [UIButtonConfiguration tintedButtonConfiguration];
    scanConfig.image = [UIImage systemImageNamed:@"qrcode.viewfinder"
        withConfiguration:[UIImageSymbolConfiguration configurationWithPointSize:18 weight:UIImageSymbolWeightSemibold]];
    scanConfig.baseForegroundColor = RG_RING;
    scanConfig.baseBackgroundColor = RG_RING;
    self.scanButton = [UIButton buttonWithConfiguration:scanConfig primaryAction:nil];
    [self.scanButton addTarget:self action:@selector(scanTapped) forControlEvents:UIControlEventTouchUpInside];
    self.scanButton.accessibilityLabel = @"扫码连接";
    [self.scanButton.widthAnchor constraintEqualToConstant:64].active = YES;
    UIStackView *addressRow = [[UIStackView alloc] initWithArrangedSubviews:@[self.addressField, self.scanButton]];
    addressRow.spacing = 8;

    self.modeControl = [[UISegmentedControl alloc] initWithItems:@[@"远程服务器", @"局域网 Wi‑Fi"]];
    self.modeControl.selectedSegmentTintColor = RG_LIVE;
    [self.modeControl setTitleTextAttributes:@{NSForegroundColorAttributeName: RG_MUTED,
        NSFontAttributeName: [UIFont systemFontOfSize:14 weight:UIFontWeightSemibold]} forState:UIControlStateNormal];
    [self.modeControl setTitleTextAttributes:@{NSForegroundColorAttributeName: UIColor.whiteColor,
        NSFontAttributeName: [UIFont systemFontOfSize:14 weight:UIFontWeightSemibold]} forState:UIControlStateSelected];
    [self.modeControl addTarget:self action:@selector(modeChanged:) forControlEvents:UIControlEventValueChanged];
    [self.modeControl.heightAnchor constraintEqualToConstant:36].active = YES;

    self.addressCaption = [self captionWithText:@"服务器地址"];
    self.keyCaption = [self captionWithText:@"连接 Key"];
    self.formStack = [[UIStackView alloc] initWithArrangedSubviews:@[
        self.addressCaption, addressRow,
        self.keyCaption, keyRow]];
    self.formStack.axis = UILayoutConstraintAxisVertical;
    self.formStack.spacing = 6;
    [self.formStack setCustomSpacing:14 afterView:addressRow];

    UIButtonConfiguration *connectConfig = [UIButtonConfiguration filledButtonConfiguration];
    connectConfig.title = @"连接";
    connectConfig.baseBackgroundColor = RG_LIVE;
    connectConfig.baseForegroundColor = UIColor.whiteColor;
    connectConfig.cornerStyle = UIButtonConfigurationCornerStyleLarge;
    connectConfig.titleTextAttributesTransformer = ^NSDictionary *(NSDictionary *incoming) {
        NSMutableDictionary *out = [incoming mutableCopy];
        out[NSFontAttributeName] = [UIFont systemFontOfSize:17 weight:UIFontWeightBold];
        return out;
    };
    self.connectButton = [UIButton buttonWithConfiguration:connectConfig primaryAction:nil];
    [self.connectButton addTarget:self action:@selector(connectTapped) forControlEvents:UIControlEventTouchUpInside];
    [self.connectButton.heightAnchor constraintEqualToConstant:52].active = YES;

    self.hintLabel = [self captionWithText:@"轻点对焦后上下滑调亮度 · 长按锁定对焦和亮度 · 双击切换前后摄像头"];
    self.hintLabel.textAlignment = NSTextAlignmentCenter;
    self.hintLabel.numberOfLines = 0;
    self.hintLabel.font = [UIFont systemFontOfSize:11];

    UIStackView *panelStack = [[UIStackView alloc] initWithArrangedSubviews:@[self.modeControl, self.formStack, self.connectButton, self.hintLabel]];
    panelStack.axis = UILayoutConstraintAxisVertical;
    panelStack.spacing = 14;
    panelStack.translatesAutoresizingMaskIntoConstraints = NO;
    [self.panel addSubview:panelStack];

    UILayoutGuide *safe = self.view.safeAreaLayoutGuide;
    [NSLayoutConstraint activateConstraints:@[
        [self.statusPill.topAnchor constraintEqualToAnchor:safe.topAnchor constant:8],
        [self.statusPill.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor constant:14],
        [self.statusPill.heightAnchor constraintEqualToConstant:32],
        [self.statusPill.trailingAnchor constraintLessThanOrEqualToAnchor:safe.trailingAnchor constant:-14],
        [self.statusDot.leadingAnchor constraintEqualToAnchor:self.statusPill.leadingAnchor constant:12],
        [self.statusDot.centerYAnchor constraintEqualToAnchor:self.statusPill.centerYAnchor],
        [self.statusDot.widthAnchor constraintEqualToConstant:8],
        [self.statusDot.heightAnchor constraintEqualToConstant:8],
        [self.statusLabel.leadingAnchor constraintEqualToAnchor:self.statusDot.trailingAnchor constant:8],
        [self.statusLabel.trailingAnchor constraintEqualToAnchor:self.statusPill.trailingAnchor constant:-14],
        [self.statusLabel.centerYAnchor constraintEqualToAnchor:self.statusPill.centerYAnchor],

        [self.statsLabel.topAnchor constraintEqualToAnchor:self.statusPill.bottomAnchor constant:6],
        [self.statsLabel.leadingAnchor constraintEqualToAnchor:self.statusPill.leadingAnchor constant:4],

        [self.lockLabel.centerXAnchor constraintEqualToAnchor:self.view.centerXAnchor],
        [self.lockLabel.topAnchor constraintEqualToAnchor:safe.topAnchor constant:52],
        [self.lockLabel.heightAnchor constraintEqualToConstant:26],

        [side.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-14],
        [side.bottomAnchor constraintEqualToAnchor:self.panel.topAnchor constant:-16],

        [self.panel.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor constant:12],
        [self.panel.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-12],
        [self.panel.bottomAnchor constraintEqualToAnchor:self.view.keyboardLayoutGuide.topAnchor constant:-10],
        [panelStack.topAnchor constraintEqualToAnchor:self.panel.topAnchor constant:16],
        [panelStack.leadingAnchor constraintEqualToAnchor:self.panel.leadingAnchor constant:16],
        [panelStack.trailingAnchor constraintEqualToAnchor:self.panel.trailingAnchor constant:-16],
        [panelStack.bottomAnchor constraintEqualToAnchor:self.panel.bottomAnchor constant:-12],
    ]];
}

#pragma mark UITextFieldDelegate

- (BOOL)textFieldShouldReturn:(UITextField *)textField {
    if (textField == self.addressField) [self.keyField becomeFirstResponder];
    else { [textField resignFirstResponder]; [self connectTapped]; }
    return YES;
}

@end
