# 다른 PC·채팅에서 이어서 작업하기

## 가져오기와 준비

지원 환경은 Windows 10/11 x64, Git, Python 3.12 x64(검증 기준)다. Python 3.11 이상을 허용한다.
JavaScript 테스트에는 Node.js 22 이상이 필요하고, 앱 실행에는 Node.js가 필요하지 않다.
인터넷 연결로 Python 패키지와 아래 고정 센서 배포본을 가져온다.

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
.\.venv\Scripts\python.exe -m unittest test_onboarding test_governor_http test_monitor test_system_metrics test_temperatures
node --test test_speedtest.mjs
# 전체 Python 검사 (아래 알려진 실패 참고)
.\.venv\Scripts\python.exe -m unittest discover
```

GitHub Actions는 Windows에서 위의 명시된 Python 검사, JavaScript 검사, 실행 파일 빌드를 수행한다.
수동 실행 또는 main push/PR로 실행된다. 성공은 실제 PC의 센서·GPU 드라이버 검증을 대체하지 않는다.
전체 검사 중 `test_resource_ipc.FileIPCTests.test_concurrent_file_requests_preserve_one_heavy_lease`는
40개 동시 요청의 시간 제한 때문에 이 PC의 변경 전 기준 커밋에서도 실패했다. 해결되지 않은 별도 항목이다.

## 현재 인계 상태 (2026-10-20)

- 앱 버전: 0.5.0. CPU/GPU/RAM/디스크 모델·온도·상태 색상, 네트워크 연결 종류/실시간 사용량,
  버튼으로 실행하는 다운로드·업로드 속도 테스트 구현 완료.
- 안정 분배는 사용자가 켤 때만 작동한다. 속도 테스트 결과나 사용량만으로 가입 회선 상품을 단정하지 않는다.
- 이번 작업: clone 후 누락되던 센서 준비를 setup/build에 연결하고, 저장·복원·인계 절차를 저장소에 포함했다.
- 검증: 별도 GitHub clone + 새 가상환경에서 센서 다운로드/해시 확인·Windows 실행 파일 빌드 성공.
  Python 31개·JavaScript 5개 검사를 기준으로 확인한다. 자동 Windows 환경에서 발견한 JavaScript MIME 형식 차이도 수정했다.
- 기존 PC의 실행 인스턴스는 이번 정리 작업에서 종료하거나 교체하지 않는다.
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
