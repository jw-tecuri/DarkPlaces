DarkPlaces macOS arm64 (unofficial)

This is an unofficial build from the jw-tecuri/DarkPlaces fork of
DarkPlacesEngine/DarkPlaces. It is ad-hoc signed (codesign --sign -) and is
not notarized. Gatekeeper will refuse to open a downloaded copy until you
right-click the app and choose Open, or clear the quarantine flag:

  xattr -dr com.apple.quarantine DarkPlaces.app

No Quake game data is included.

Where the app looks for game data
---------------------------------
On macOS, if the executable path contains ".app/", DarkPlaces checks for
<app>/Contents/Resources/id1. When that directory exists, Contents/Resources
is the base directory. When it does not, the base directory is the folder
that contains DarkPlaces.app. Put the commercial or freely licensed game
data next to the app:

  /Games/Quake/DarkPlaces.app
  /Games/Quake/id1/pak0.pak

Or pass the directory that contains id1 (or data/, for other games):

  open DarkPlaces.app --args -basedir /Games/Quake
  DarkPlaces.app/Contents/MacOS/darkplaces-sdl -basedir /Games/Quake

Configs and saves go to ~/Library/Application Support/darkplaces/ unless you
pass -nohome. -userdir <path> overrides that location.

The dedicated server is in the dedicated/ directory of the zip (the DMG
contains only the app and an /Applications shortcut). Its dylibs sit beside
the binary because that is the first place DarkPlaces dlopens them. It does
not use the .app base-directory logic:

  ./dedicated/darkplaces-dedicated -dedicated -basedir /Games/Quake

macOS still ships OpenGL, but it is deprecated. This build requests an
OpenGL 3.2 Core context. The newest OpenGL profile macOS provides is 4.1.

JPG, PNG, Ogg Vorbis, Theora, FreeType, libxmp and curl are dlopen'd.
libjpeg is the v6b ABI (libjpeg.62.dylib). Homebrew's default libjpeg-turbo
bottle is the v8 ABI and is not what this engine loads. Those libraries live
in DarkPlaces.app/Contents/Frameworks, and the engine also looks there
(../Frameworks relative to Contents/MacOS).
