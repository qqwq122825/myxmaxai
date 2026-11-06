// Instantiate the DLL directly: no driver installation and no user's camera is opened.
#include <windows.h>
#include <dshow.h>
#include <cstdio>
#include <cstdlib>
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "strmiids.lib")

#define REQUIRE(x) do { if (!(x)) { printf("FAIL line %d: %s\n", __LINE__, #x); return 1; } } while (0)

int wmain(int argc, wchar_t** argv)
{
    REQUIRE(argc == 2 || argc == 3);
    REQUIRE(SUCCEEDED(CoInitializeEx(NULL, COINIT_MULTITHREADED)));
    HMODULE dll = LoadLibraryW(argv[1]);
    REQUIRE(dll != NULL);
    typedef HRESULT (STDAPICALLTYPE* FactoryProc)(REFCLSID, REFIID, LPVOID*);
    FactoryProc getFactory = (FactoryProc)GetProcAddress(dll, "DllGetClassObject");
    REQUIRE(getFactory != NULL);
    // Default is the isolated third channel. An explicit CLSID can check a
    // separately built camera variant, also without registering any device.
    CLSID clsid;
    REQUIRE(SUCCEEDED(CLSIDFromString(argc == 3 ? argv[2] : L"{5C2CD55C-92AD-4999-8666-912BD3E70013}", &clsid)));
    IClassFactory* factory = NULL;
    REQUIRE(SUCCEEDED(getFactory(clsid, IID_IClassFactory, (void**)&factory)));
    IBaseFilter* source = NULL;
    REQUIRE(SUCCEEDED(factory->CreateInstance(NULL, IID_IBaseFilter, (void**)&source)));
    factory->Release();
    IEnumPins* pins = NULL;
    IPin* pin = NULL;
    IAMStreamConfig* config = NULL;
    REQUIRE(SUCCEEDED(source->EnumPins(&pins)));
    REQUIRE(pins->Next(1, &pin, NULL) == S_OK);
    REQUIRE(SUCCEEDED(pin->QueryInterface(IID_IAMStreamConfig, (void**)&config)));
    int count = 0, capSize = 0;
    REQUIRE(SUCCEEDED(config->GetNumberOfCapabilities(&count, &capSize)));
    REQUIRE(count > 2 && capSize == sizeof(VIDEO_STREAM_CONFIG_CAPS));
    for (int i = 0; i < count; ++i)
    {
        AM_MEDIA_TYPE* option = NULL;
        VIDEO_STREAM_CONFIG_CAPS advertised;
        REQUIRE(SUCCEEDED(config->GetStreamCaps(i, &option, (BYTE*)&advertised)));
        REQUIRE(advertised.MaxBitsPerSecond > 0 && advertised.MaxBitsPerSecond >= advertised.MinBitsPerSecond);
        if (option->pUnk) option->pUnk->Release();
        CoTaskMemFree(option->pbFormat); CoTaskMemFree(option);
    }
    AM_MEDIA_TYPE* mt = NULL;
    VIDEO_STREAM_CONFIG_CAPS caps;
    REQUIRE(config->GetStreamCaps(count, &mt, (BYTE*)&caps) == S_FALSE && mt == NULL);
    REQUIRE(FAILED(config->GetStreamCaps(-1, &mt, (BYTE*)&caps)) && mt == NULL);
    REQUIRE(SUCCEEDED(config->GetStreamCaps(0, &mt, (BYTE*)&caps)));
    REQUIRE(caps.MaxFrameInterval >= 10000000 / 25);
    VIDEOINFOHEADER* vi = (VIDEOINFOHEADER*)mt->pbFormat;
    vi->bmiHeader.biWidth = 960;
    vi->bmiHeader.biHeight = 540;
    vi->bmiHeader.biSizeImage = 0; // Clients may omit this advisory field for uncompressed video.
    vi->AvgTimePerFrame = 10000000 / 25;
    vi->dwBitRate = 987654;
    SetRect(&vi->rcSource, 0, 0, 960, 540);
    SetRect(&vi->rcTarget, 0, 0, 960, 540);
    REQUIRE(pin->QueryAccept(mt) == S_OK);
    REQUIRE(SUCCEEDED(config->SetFormat(mt)));
    AM_MEDIA_TYPE* selected = NULL;
    REQUIRE(SUCCEEDED(config->GetFormat(&selected)));
    VIDEOINFOHEADER* actual = (VIDEOINFOHEADER*)selected->pbFormat;
    REQUIRE(actual->bmiHeader.biWidth == 960 && actual->bmiHeader.biHeight == 540);
    REQUIRE(actual->bmiHeader.biSizeImage == 960 * 540 * 3);
    REQUIRE(actual->AvgTimePerFrame == 10000000 / 25);
    IEnumMediaTypes* offered = NULL;
    REQUIRE(SUCCEEDED(pin->EnumMediaTypes(&offered)));
    AM_MEDIA_TYPE* next = NULL;
    REQUIRE(offered->Next(1, &next, NULL) == S_OK);
    VIDEOINFOHEADER* preferred = (VIDEOINFOHEADER*)next->pbFormat;
    REQUIRE(preferred->bmiHeader.biWidth == 960 && preferred->bmiHeader.biHeight == 540);
    REQUIRE(preferred->bmiHeader.biBitCount == 24 && preferred->AvgTimePerFrame == 10000000 / 25);
    if (next->pUnk) next->pUnk->Release();
    CoTaskMemFree(next->pbFormat); CoTaskMemFree(next); next = NULL;
    REQUIRE(offered->Next(1, &next, NULL) == S_FALSE && next == NULL);
    offered->Release();
    vi->bmiHeader.biHeight = -540;
    REQUIRE(pin->QueryAccept(mt) != S_OK); // Negative height would break the bottom-up pixel path.
    vi->bmiHeader.biHeight = 540;
    vi->bmiHeader.biBitCount = 16;
    REQUIRE(pin->QueryAccept(mt) != S_OK);
    vi->bmiHeader.biBitCount = 24;
    vi->bmiHeader.biWidth = 959;
    REQUIRE(pin->QueryAccept(mt) != S_OK); // Padded RGB24 rows must not be treated as packed.
    vi->bmiHeader.biWidth = 960;
    vi->bmiHeader.biSizeImage = 7;
    REQUIRE(pin->QueryAccept(mt) != S_OK);
    vi->bmiHeader.biSizeImage = 0;
    vi->AvgTimePerFrame = 0;
    REQUIRE(pin->QueryAccept(mt) != S_OK);
    vi->AvgTimePerFrame = 10000000 / 30;
    vi->bmiHeader.biBitCount = 32;
    mt->subtype = MEDIASUBTYPE_RGB32;
    REQUIRE(SUCCEEDED(config->SetFormat(mt)));
    REQUIRE(pin->QueryAccept(mt) == S_OK);
    printf("PASS: multiple modes, 25fps, RGB24/RGB32, advisory fields, malformed layouts rejected\n");
    if (mt->pUnk) mt->pUnk->Release();
    CoTaskMemFree(mt->pbFormat); CoTaskMemFree(mt);
    if (selected->pUnk) selected->pUnk->Release();
    CoTaskMemFree(selected->pbFormat); CoTaskMemFree(selected);
    config->Release(); pin->Release(); pins->Release(); source->Release();
    CoUninitialize();
    // DirectShow worker teardown can retain module state until process exit.
    return 0;
}
