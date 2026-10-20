# 다른 PC의 CPU·메모리 온도 측정 준비

이 폴더에는 **공식 PawnIO 2.1.0 설치 파일**을 수정 없이 보관합니다.
자원 분석기가 사용하는 LibreHardwareMonitor 0.9.6의 고정 설치 파일과 동일합니다.
설치 파일이 있으므로 PawnIO를 다시 다운로드할 필요가 없습니다.
Python·Python 패키지·LibreHardwareMonitor 빌드 자료까지 포함한 완전한 오프라인 개발 환경은 아닙니다.

## 설치

1. `PawnIO_setup.exe`를 실행하고 Windows 관리자 승인 화면에서 허용합니다.
2. 일반 공식 드라이버를 설치합니다. 개발자용 unrestricted/unsigned 항목은 선택하지 않습니다.
3. 설치 후 자원 분석기를 다시 실행하고 온도 센서의 관리자 실행도 승인합니다.
4. 설치 프로그램이 재시작을 요구하면 PC를 재시작한 뒤 확인합니다.

설치 파일은 Windows x64·ARM64용 드라이버를 포함합니다. 호환 Windows 버전은
공식 설치 프로그램이 검사합니다. 설치가 완료되어도 부품·펌웨어가 센서를 제공하지 않는 경우
온도는 미측정으로 남습니다. GPU 드라이버 온도와 CPU·DIMM용 PawnIO 조건은 서로 다릅니다.

관리자가 직접 명령으로 설치할 때 공식 무인 설치 옵션은 다음과 같습니다.

```powershell
.\PawnIO_setup.exe -install -silent
```

이 저장소의 `setup.ps1`과 `build.ps1`은 설치 파일을 준비·복사할 뿐 드라이버를 설치하지 않습니다.
명령 옵션 근거:
https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/discussions/1904

## 파일 검증과 출처

- 공식 릴리스: https://github.com/namazso/PawnIO.Setup/releases/tag/2.1.0
- 원본 설치 파일: https://github.com/namazso/PawnIO.Setup/releases/download/2.1.0/PawnIO_setup.exe
- 설치 파일 버전: `2.1.0.0`
- 파일 크기: `3,225,016` 바이트
- 설치 파일 SHA-256: `a3a46226c5e2824f4cdd42be0eecbabfc672c86f7889710f5ab1e6ad385b47a0`
- 확인한 Authenticode 서명자: `namazso.eu` / `namazso`

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath .\PawnIO_setup.exe
Get-AuthenticodeSignature -LiteralPath .\PawnIO_setup.exe
```

`SOURCES.json`에는 각 배포 파일의 원본 URL·정확한 소스 커밋·크기·SHA-256이 있습니다.
`SHA256SUMS.txt`로 설치 파일·소스 ZIP·라이선스 자료를 함께 검증할 수 있습니다.

## 재배포와 소스

설치 프로그램에 포함된 배포 고지는 `PawnIO-Setup-NOTICE.txt`에 보관합니다.
설치 프로그램은 수정하지 않고 재배포합니다.
포함 드라이버의 GPL-2.0-or-later 및 예외 조항은 `PawnIO-COPYING.txt`와
`PawnIO-LICENSE-NOTICE.txt`를 함께 확인하십시오.

- `PawnIO-2.1.0-source.zip`: 드라이버·PawnIOLib·PawnIOUtil의 공식 2.1.0 소스입니다.
- `PawnPP-e64e4c3-source.zip`: 위 버전이 고정한 PawnPP 하위 모듈의 소스입니다.
- `PawnPP-LICENSE.txt`: PawnPP의 MPL-2.0 라이선스입니다.

소스 복원 시 두 ZIP을 풉니다. PawnIO 소스 루트의 `PawnPP` 폴더에
PawnPP ZIP의 최상위 폴더 **안에 있는 내용**을 넣으면 원본 `.gitmodules`의
하위 모듈 배치가 복원됩니다. 원본 `CMakeLists.txt`와 빌드 파일도 ZIP에 들어 있습니다.
소스 빌드에는 CMake 3.25 이상과 Microsoft C++/Windows Driver Kit 도구가 필요합니다.
소스를 빌드한 결과물은 제조사의 서명된 배포 파일과 같다고 보장하지 않습니다.
일반 설치에는 위의 서명된 `PawnIO_setup.exe`를 사용하십시오.

`PawnIO.Setup` 저장소는 공식 릴리스 배포용이며 설치 래퍼의 소스는 게시하지 않습니다.
여기에 포함한 ZIP은 공식 공개된 **드라이버 2.1.0 및 그 하위 모듈의 소스**입니다.
이 폴더를 다른 곳에 배포할 때 설치 파일만 떼어 내지 말고 소스 ZIP과 라이선스·출처를
포함한 폴더 전체를 함께 전달하십시오.
