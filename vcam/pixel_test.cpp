// Exercise the production converter with synthetic RGBA frames; no device is registered.
#define _HAS_EXCEPTIONS 0
#include <vector>
#include <cstdio>
#define FACELIVE_PIXEL_TEST
#include "UnityCaptureFilter.cpp"

struct PixelTest
{
    static bool Run(int iw, int ih, int ow, int oh, int bpp, int padding)
    {
        HRESULT hr = S_OK;
        CSource* owner = new CSource("Pixel test", NULL, CLSID_NULL, &hr);
        CCaptureStream* stream = new CCaptureStream(owner, &hr, 2);
        const int stride = iw + padding, guard = 64;
        std::vector<uint8_t> input((size_t)stride * ih * 4, 0xAB);
        for (int y = 0; y < ih; ++y)
            for (int x = 0; x < iw; ++x)
            {
                uint8_t* p = &input[((size_t)y * stride + x) * 4];
                p[0] = (x * 13 + y * 7) % 256;
                p[1] = (x * 3 + y * 11) % 256;
                p[2] = (x * 5 + y * 17) % 256;
                p[3] = 255;
            }
        std::vector<uint8_t> output((size_t)ow * oh * bpp + guard * 2, 0xCD);
        CCaptureStream::ProcessState state = { output.data() + guard, ow, oh, bpp, stream };
        CCaptureStream::ProcessImage(iw, ih, stride, SharedImageMemory::FORMAT_UINT8,
            SharedImageMemory::RESIZEMODE_LINEAR, SharedImageMemory::MIRRORMODE_DISABLED, 1000, input.data(), &state);
        const double scale = max(iw / (double)ow, ih / (double)oh);
        const double ax = (ow - iw / scale) / 2.0, ay = (oh - ih / scale) / 2.0;
        size_t bad = 0;
        for (int y = 0; y < oh; ++y)
            for (int x = 0; x < ow; ++x)
            {
                const double sx = (x - ax) * scale, sy = (y - ay) * scale;
                uint8_t expected[4] = {0, 0, 0, 0};
                if (sx >= 0 && sy >= 0 && sx < iw && sy < ih)
                {
                    const uint8_t* p = &input[((size_t)sy * stride + (size_t)sx) * 4];
                    expected[0] = p[2]; expected[1] = p[1]; expected[2] = p[0]; expected[3] = p[3];
                }
                const uint8_t* p = output.data() + guard + ((size_t)y * ow + x) * bpp;
                for (int c = 0; c < bpp; ++c) if (p[c] != expected[c]) { ++bad; break; }
            }
        bool guards = true;
        for (int i = 0; i < guard; ++i)
            if (output[i] != 0xCD || output[output.size() - 1 - i] != 0xCD) guards = false;
        printf("%dx%d stride=%d -> %dx%d RGB%d: mismatched_pixels=%llu guards=%s\n",
            iw, ih, stride, ow, oh, bpp * 8, (unsigned long long)bad, guards ? "ok" : "BAD");
        // Also exercise the real shared-memory -> FillBuffer -> IMediaSample path.
        CMediaType mt;
        stream->GetAvailableMediaType(0, &mt);
        VIDEOINFOHEADER* vi = (VIDEOINFOHEADER*)mt.Format();
        vi->bmiHeader.biWidth = ow; vi->bmiHeader.biHeight = oh;
        vi->bmiHeader.biBitCount = bpp * 8;
        vi->bmiHeader.biSizeImage = ow * oh * bpp;
        mt.SetSubtype(bpp == 3 ? &MEDIASUBTYPE_RGB24 : &MEDIASUBTYPE_RGB32);
        mt.SetSampleSize(ow * oh * bpp);
        bool sampleOK = SUCCEEDED(stream->SetFormat(&mt));
        CMemAllocator* allocator = new CMemAllocator("Pixel allocator", NULL, &hr);
        allocator->AddRef();
        ALLOCATOR_PROPERTIES request = {1, ow * oh * bpp, 1, 0}, actual;
        sampleOK = sampleOK && SUCCEEDED(allocator->SetProperties(&request, &actual)) && SUCCEEDED(allocator->Commit());
        IMediaSample* sample = NULL;
        sampleOK = sampleOK && SUCCEEDED(allocator->GetBuffer(&sample, NULL, NULL, 0));
        if (sample)
        {
            SharedImageMemory producer(2);
            int attempts = 0;
            while (!producer.SendIsReady() && attempts++ < 5) stream->FillBuffer(sample);
            sampleOK = sampleOK && producer.SendIsReady();
            if (sampleOK)
            {
                producer.Send(iw, ih, stride, (DWORD)input.size(), SharedImageMemory::FORMAT_UINT8,
                    SharedImageMemory::RESIZEMODE_LINEAR, SharedImageMemory::MIRRORMODE_DISABLED, 1000, input.data());
                sampleOK = SUCCEEDED(stream->FillBuffer(sample));
                BYTE* pixels = NULL;
                sampleOK = sampleOK && SUCCEEDED(sample->GetPointer(&pixels)) && sample->GetActualDataLength() == ow * oh * bpp;
                sampleOK = sampleOK && memcmp(pixels, output.data() + guard, ow * oh * bpp) == 0;
            }
            sample->Release();
        }
        allocator->Decommit(); allocator->Release();
        printf("  shared memory / IMediaSample: %s\n", sampleOK ? "PASS" : "FAIL");
        delete owner;
        return bad == 0 && guards && sampleOK;
    }
};

int main()
{
    const int sizes[][4] = {{1280,720,1280,720}, {1280,720,960,540},
        {720,1280,1280,720}, {720,1280,960,540}, {720,1280,720,1280}};
    bool ok = true;
    for (int i = 0; i < 5; ++i)
        for (int bpp = 3; bpp <= 4; ++bpp)
            for (int padding = 0; padding <= 8; padding += 8)
                ok = PixelTest::Run(sizes[i][0], sizes[i][1], sizes[i][2], sizes[i][3], bpp, padding) && ok;
    return ok ? 0 : 1;
}
