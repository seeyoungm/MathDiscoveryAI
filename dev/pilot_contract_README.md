# Week 3 메뉴 계약 복구

기존 Week 3 증거와 재생 기록은 수정하지 않았습니다. 새 모듈은 보존된 프롬프트·응답과 개발 매니페스트의 바이트 SHA-256을 확인한 뒤, 프롬프트에 실제로 인쇄한 순서만 해석합니다. B0·B1의 인덱스 0은 현재 후보 목록의 첫 항목이 아니라, 원래 응답에 허용했던 중립적인 이중 부정입니다.

```sh
cd dev
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_pilot_contract.py -v
# 기존 증거를 덮지 않는 별도 재현용 복사본에서만:
PYTHONDONTWRITEBYTECODE=1 python3 -m mdai.pilot_contract --out evidence/pilot_contract_repair
```

`historical_binding.json`은 원본 기록 인덱스와 검증된 회로 후보를 연결합니다. `preregistered.json`은 고정 구성입니다. `summary.json`과 개별 `contract.json`, `receipt.json`은 각 실행의 메뉴 및 계측을 기록합니다. 합성 콜백과 무모델 기준은 같은 메뉴를 사용합니다. 이전 ABC 포트폴리오, 실제 라이브 모델, 비용 비교, 학습, 평가 세트는 이 실험에 들어가지 않습니다. 내부 공용 러너는 콜백 경로를 `live`라고 부르지만 개별 계약 영수증과 요약에 합성 콜백이며 모델 호출이 0임을 명시합니다.
