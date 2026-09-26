@echo off
REM Шаг 1 из 2: сборка JARVIS в один .exe файл (Windows).
REM Запускать из папки, где лежит jarvis_system_agent_fixed.py.
REM
REM Если положишь рядом файл icon.ico (твоя иконка, когда пришлёшь) —
REM он автоматически подхватится и для .exe, и потом для установщика.

pip install pyinstaller

set ICON_FLAG=
if exist icon.ico set ICON_FLAG=--icon=icon.ico

pyinstaller --onefile --name Jarvis %ICON_FLAG% ^
  --hidden-import=comtypes.stream ^
  --hidden-import=pycaw.pycaw ^
  --hidden-import=speech_recognition ^
  --collect-all pyttsx3 ^
  --collect-all edge_tts ^
  --hidden-import=playsound ^
  jarvis_system_agent_fixed.py

echo.
echo Готово. Файл: dist\Jarvis.exe
echo Дальше: открой setup.iss в Inno Setup Compiler и нажми Compile —
echo получится JarvisSetup.exe (полноценный установщик).
pause
