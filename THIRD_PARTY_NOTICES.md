# Third-party notices

DiscDock is an independent implementation whose behavior and feature set were informed by Automatic Ripping Machine:

> The MIT License (MIT)
>
> Copyright (c) 2016 Benjamin Bryan  
> Copyright (c) 2022 Andrew Sneed
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

DiscDock runs separately installed applications, including MakeMKV, VLC, FFmpeg, HandBrake and cyanrip. They are not included in this repository or in the DiscDock installer, and each has its own license.

## Bundled components

The Windows release includes Python and these open-source libraries, each under its own license:

- Python (PSF License)
- FastAPI (MIT), Starlette (BSD-3-Clause), Uvicorn (BSD-3-Clause), Pydantic (MIT), HTTPX (BSD-3-Clause)
- Apprise (BSD-2-Clause), psutil (BSD-3-Clause), pywin32 (PSF License)

The dashboard is built with Next.js (MIT), React (MIT), Tailwind CSS (MIT), Radix UI (MIT), shadcn/ui (MIT) and Lucide icons (ISC). The installer is built with Inno Setup.
