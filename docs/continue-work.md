# 다른 PC·채팅에서 이어서 작업하기

## Qwen 연동 — 2026-10-21

0.6.0에서 Qwen 화면 분석/요약 단계를 등록했다. [분배 정책·실측·적용 범위](qwen-governor.md)를 먼저 읽는다.
LEARN & WORK의 대응 클라이언트도 필요하다. 이전에 실행 중이던 프로세스의 적용 여부는 별도 확인해야 한다.
이 PC에서는 E 설치 백업을 사용한다. 아래 과거의 E 미연결 기록은 다른 검증 환경의 기록이다.

## 가져오기와 준비

지원 환경은 Windows 10/11 x64, Git, Python 3.12 x64(검증 기준)다. Python 3.11 이상을 허용한다.
JavaScript 테스트에는 Node.js 22 이상이 필요하고, 앱 실행에는 Node.js가 필요하지 않다.
인터넷 연결로 Python 패키지와 아래 고정 센서 배포본을 가져온다.
공식 PawnIO 2.1.0 설치 파일·대응 소스·라이선스는 `installers/`에 포함되어 별도 다운로드 없이 사용할 수 있다.

```powershell
git clone https://github.com/brownrigoon-commits/resource-analyzer.git
cd resource-analyzer
git status --short
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
.\.venv\Scripts\python.exe entrypoint.py
```

`python`이 다른 버전이면 `setup.ps1 -Python <Python-3.12-python.exe>`를 사용한다.
준비 스크립트는 의존성 설치, 해시로 확인한 센서 DLL 준비, Windows에 포함된 .NET Framework 컴파일러로
SensorBridge 빌드까지 수행한다. 드라이버 설치·관리자 프로세스 실행·기존 분석기 재시작은 하지 않는다.
전체 앱 빌드는 다음 한 명령으로 준비 단계까지 수행한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
# dist/ResourceAnalyzer.exe, dist/SHA256SUMS.txt
# dist/BUILD-INFO.txt (버전·소스 커밋·미커밋 변경 여부·시각·실행 파일 해시)
# dist/BUILD-DEPENDENCIES.txt (해당 빌드에 사용한 Python 패키지 버전)
```

같은 PC에서 실행 중인 분석기가 있으면 기본 포트 7442로 기존 화면을 연다.
독립 검증에는 다른 포트와 데이터 폴더를 **둘 다** 지정한다.

```powershell
.\.venv\Scripts\python.exe entrypoint.py --port 17442 --data-dir .\local\smoke --no-browser
# 브라우저에서 http://127.0.0.1:17442/ 를 연다.
# 검증 인스턴스만 종료:
.\.venv\Scripts\python.exe entrypoint.py --port 17442 --data-dir .\local\smoke --stop --no-browser
```

기록은 기본적으로 `%LOCALAPPDATA%\ResourceAnalyzer`에 저장된다. 새 PC에서 기록이 비어 있는 것은 정상이다.
기록·비밀 설정은 소스 복제에 포함되지 않는다. 이 앱에는 API 키나 로컬 ML 모델이 필요하지 않다.

## 무엇을 어디에 보관하는가

| 위치 | 보관 내용 | 복원 방법 |
| --- | --- | --- |
| 이 GitHub 저장소 | 전체 앱 소스/UI, 라이선스, 고정 직접 의존성, 준비/빌드 도구, 테스트, 작업 인계 문서 | clone 후 위 명령 실행 |
| GitHub Actions 빌드 산출물 | Windows 실행 파일과 SHA-256; 보관 기한이 있으므로 영구 백업이 아님 | Actions의 성공한 빌드에서 다운로드 |
| GitHub Releases | 게시된 버전의 실행 파일과 체크섬; 최신 main과 같다고 가정하지 않음 | 사용할 버전과 소스 커밋 확인 |
| `E:\RIGOON_STUDIO\PC_INSTALL_BACKUPS\resource-analyzer` | 버전별 실행/설치 파일, 드라이버, 센서 DLL/공식 원본, Git 소스 bundle, manifest와 복원 안내 | `LATEST.txt`가 가리키는 폴더의 `RESTORE.txt`부터 읽기 |

E드라이브의 설치 파일은 Git에 추가하지 않는다. 새 PC에 E드라이브가 없으면 공식 원본으로 다시 준비할 수 있다.
온도 엔진은 LibreHardwareMonitor 0.9.6 + PawnIO이며 공식 URL과 SHA-256은
`tools/build_sensor_bridge.py`에 고정되어 있다. E 사본을 재사용하려면 setup/build 명령에
`-SensorArchive <LibreHardwareMonitor.zip> -PawnIOInstaller <PawnIO_setup.exe>`를 추가한다.
Cloudflare 속도 테스트 1.14.2의 브라우저 모듈과 라이선스는 Git에 포함되어 있고,
재취득 도구의 공식 URL·해시는 `tools/vendor_speedtest.py`에 있다.
Python 직접 의존성은 `requirements*.txt`에 고정되어 있다. 간접 패키지까지 바이트 단위로 같은 빌드를 보장하는 잠금 파일은 아니다.

CPU/DIMM 온도는 새 PC에서도 별도 PawnIO 설치와 관리자 승인이 필요하다.
`dist\ResourceAnalyzer.exe --install-temperature-support`로 설치할 수 있다.
NVIDIA GPU는 해당 PC에 맞는 NVIDIA 드라이버가 필요하다. 이 절차는 드라이버를 자동 설치하지 않는다.
하드웨어/드라이버가 센서를 제공하지 않으면 미측정으로 표시한다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m unittest test_onboarding test_governor_http test_monitor test_system_metrics test_temperatures test_gpu_windows
node --test test_speedtest.mjs
# 전체 Python 검사 (아래 알려진 실패 참고)
.\.venv\Scripts\python.exe -m unittest discover
```

GitHub Actions는 Windows에서 위의 명시된 Python 검사, JavaScript 검사, 실행 파일 빌드를 수행한다.
수동 실행 또는 main push/PR로 실행된다. 성공은 실제 PC의 센서·GPU 드라이버 검증을 대체하지 않는다.
전체 검사 중 `test_resource_ipc.FileIPCTests.test_concurrent_file_requests_preserve_one_heavy_lease`는
40개 동시 요청의 시간 제한 때문에 이 PC의 변경 전 기준 커밋에서도 실패했다. 해결되지 않은 별도 항목이다.

## 현재 인계 상태 (2026-10-20)

- 앱 버전: 0.5.2. CPU/GPU/RAM/디스크 모델·온도·상태 색상, 네트워크 연결 종류/실시간 사용량,
  버튼으로 실행하는 다운로드·업로드 속도 테스트 구현 완료.
- 안정 분배는 사용자가 켤 때만 작동한다. 속도 테스트 결과나 사용량만으로 가입 회선 상품을 단정하지 않는다.
- 이번 작업: NVIDIA 전용 경로만 있어 AMD GPU가 표시되지 않던 문제를 수정했다. DXGI의 LUID로
  Windows GPU Engine/Adapter Memory 카운터를 연결하며, AMD·Intel·기타 하드웨어 공급업체에도
  같은 수집 경로를 사용한다. NVIDIA NVML/SMI는 계속 우선한다.
- Windows GPU 사용률은 엔진별 프로세스 기여도를 합한 뒤 장치에서 가장 높은 엔진을 선택한다.
  중복 인스턴스는 중복 합산하지 않으며 PID 0은 장치 집계에만 포함한다. 카운터 실패·워밍업 시
  이전 값이나 가짜 0%를 표시하지 않는다. 전용 메모리와 공유 RAM은 구분한다.
- GPU 온도는 Windows D3DKMT에서도 읽으며 DXGI LUID로 연결한다. 안정 분배의 CUDA 작업 보호는 기존 NVIDIA
  공급원 제한을 유지하며, AMD/Intel 사용량 표시가 CUDA 작업 지원을 뜻하지 않는다.
- 이전 환경 준비 검증: 별도 GitHub clone + 새 가상환경에서 센서 다운로드/해시 확인·Windows 실행 파일 빌드 성공.
  이번 GPU·온도 수정 후 명시된 Python 60개·JavaScript 5개 검사가 통과했다.
- `test_gpu_windows.py`는 제조사·LUID 분리, 중복 엔진, PID 0, 메모리 집계, 미측정·실패·워밍업,
  NVIDIA/AMD 혼합 구성, 불완전한 NVIDIA 열거, 유효한 Windows 대체값이 없을 때 native 값 보존을 검사한다.
- 이번 GPU 수정은 실제 AMD 외장·내장 GPU가 함께 있는 Windows PC에서 API와 Chrome 화면으로 확인했다.
  개인 측정값과 장치 식별자·프로그램 목록은 Git에 저장하지 않는다.
- 별도 GitHub clone과 새 빌드 가상환경에서 최종 Windows 실행 파일 빌드를 검증했다.
  설치 파일·소스·라이선스 해시는 새 checkout에서도 유지됐다. 미커밋 변경 없이 빌드한
  실행 파일을 별도 포트·데이터 폴더로 실행해 CPU·DIMM·두 GPU·디스크 온도를 확인했다.
  배포 실행 파일의 소스 커밋은 `a5888d993f66de4f79348ac2bf4578853ea8914f`이며
  이 문서의 이후 검증 결과 갱신은 실행 코드에 영향을 주지 않는다.
  실행 중인 SensorBridge는 Windows 파일 잠금 때문에 제자리에서 재컴파일할 수 없다.
- 온도 후속 수정: GPU 센서 수집을 활성화하고 CPU/DIMM의 PawnIO 전제조건과 오류를 분리했다.
  상단 GPU 온도는 읽을 수 있는 모든 GPU 센서 중 최고값이며, 실제 장치명은 별도 센서 목록에 표시한다.
  사용률 장치와 온도 장치를 이름·순서로 추측 연결하지 않는다.
- 사용자 요청으로 공식 PawnIO 2.1.0을 해시·서명 검증 후 설치했다. 설치 종료 코드 0,
  서비스 Running을 확인했다. 재시작 후 CPU·DIMM 2개·외장/내장 AMD GPU·디스크 온도가 측정됐다.
  외장 AMD GPU의 ADL 온도 요청이 거절되는 문제는 별도 Windows D3DKMT 경로로 보완했다.
- 이 PC에는 E드라이브가 연결되어 있지 않아 E 설치 파일 백업은 수행할 수 없다.
  실행 파일은 작업 출력 폴더에 보관하고, E가 연결된 PC에서 위 백업 절차를 이어간다.
- 인터넷 500Mbps/1Gbps **가입 상품**은 아직 확인되지 않았다. 공유기 로그인은 통신사 가입상품 로그인과 다르다.
- 다음 작업 시작 시 `git log -5 --oneline`, `git status`, 원격 main을 확인하고 이 문서를 갱신한다.
  정확한 소스 버전은 Git 커밋과 E manifest의 `source_commit`을 기준으로 한다.

## 작업 종료 시 보관

사용자 변경만 커밋·push하고 원격 커밋을 확인한다. 실행 가능한 빌드는 아래처럼 E에 체크포인트로 남긴다.
기존 외부 출력 폴더를 포함하는 방법은 [백업 절차](pc-install-backups.md)를 참고한다.

```powershell
.\.venv-build\Scripts\python.exe tools/backup_installables.py --package-dir dist
```

이 명령은 상주 서비스가 아니다. 실패하면 GitHub와 E 중 어떤 보관이 미완료인지 명시한다.
