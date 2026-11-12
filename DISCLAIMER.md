# 이용 안내 및 면책 고지 (Disclaimer)

이 문서는 [LICENSE](LICENSE)(MIT 라이선스)의 "있는 그대로 제공, 보증 없음, 책임 없음" 조항을 한국어로 풀어 쓰고, 데이터 복구 도구를 사용할 때의 주의사항을 안내합니다. 라이선스 조건을 추가로 제한하지 않으며, 이 문서와 LICENSE 의 영문 원문이 다르게 해석될 경우 **LICENSE 원문을 우선**합니다.

적용 대상: Disk Recover Tool(`disk_recover_tool.py` 및 이를 빌드한 실행 파일) `[v1.0]` 이후의 모든 배포본
최종 수정일: `[2026-11-12]`

## 1. 있는 그대로 제공 (보증 없음)

이 프로그램은 **무료로, 있는 그대로(AS IS) 제공**됩니다. 저작권자와 기여자는 다음을 포함해 명시적이든 묵시적이든 **어떠한 보증도 하지 않습니다.**

- 파일이 복구된다는 것, 복구된 파일의 내용이 온전하다는 것
- 프로그램에 오류가 없다는 것, 특정 목적에 적합하다는 것
- 목록·건강도(정상/의심/손상추정) 판정이 정확하다는 것
- 모든 디스크, 운영체제, 하드웨어에서 동작한다는 것

## 2. 책임의 제한

법이 허용하는 최대 범위에서, 저작권자와 기여자는 이 프로그램의 사용 또는 사용 불능으로 인해 발생하는 **어떠한 손해에 대해서도 책임을 지지 않습니다.** 예시는 다음과 같으며 이에 한정되지 않습니다.

- 데이터의 손실·훼손·덮어쓰기(복구하려던 데이터와 다른 데이터를 포함)
- 저장장치 또는 컴퓨터의 고장·상태 악화
- 잘못된 판정이나 오류로 인한 손해
- 개인정보·기밀정보의 노출(예: 보고서·CSV 파일을 공유한 경우)
- 영업 손실, 시간 손실, 기타 직접·간접·부수·결과적 손해

다만, 고의 또는 중대한 과실에 대한 책임 등 **법률상 배제할 수 없는 책임**은 해당 범위에서 이 조항의 적용을 받지 않습니다.

## 3. 이용자의 책임

- 이 프로그램을 사용할지, 어떻게 사용할지는 **이용자 스스로 판단**하고 책임집니다.
- 이용자는 **중요한 데이터를 미리 백업**하고, 복구한 파일은 **복구 대상과 다른 디스크**에 저장해야 합니다.
- 본인 소유이거나 **접근 권한이 있는 저장장치와 데이터에만** 사용하세요. 타인의 저장장치나 데이터에 권한 없이 접근하는 것은 법령에 의해 금지·처벌될 수 있으며, 그에 대한 책임은 전적으로 이용자에게 있습니다. (이는 법령 준수를 안내하는 것으로, 라이선스에 별도의 사용 제한을 추가하는 것이 아닙니다.)
- 복구한 파일은 열어서 직접 확인해야 합니다. 프로그램의 판정을 맹신하지 마세요.

## 4. 읽기 전용 설계에 대한 안내

이 프로그램은 읽고 있는 이미지나 디스크에 쓰지 않도록 설계되었습니다. 그러나 이것이 **디스크가 안전하다는 보증은 아닙니다.**

- 프로그램의 결함, 운영체제·드라이버 동작, 하드웨어 상태에 따라 예기치 않은 일이 생길 수 있습니다.
- 불량이 있는 디스크를 반복해서 읽으면 상태가 더 나빠질 수 있습니다.
- 사용 중인 드라이브를 읽는 동안 다른 프로그램·운영체제가 쓰기를 하면 복구 대상이 덮어쓰이거나 목록이 실제와 달라질 수 있습니다.
- 복사 기능은 **사용자가 지정한 저장 위치에 파일을 씁니다.** 저장 위치 선택은 이용자의 책임입니다.
- **중요한 데이터이거나 디스크가 불안정하다면 사용을 멈추고 전문 복구 업체에 문의하세요.**

## 5. 개인정보와 데이터

- 이 버전의 소스 코드에는 네트워크로 데이터를 보내는 기능이 없습니다(소스를 직접 확인할 수 있습니다). 읽은 데이터는 이용자가 지정한 위치 외에는 저장되지 않습니다.
- 목록 CSV, 복사 보고서, 스캔 캐시에는 **파일 이름, 경로, 크기, 시간 정보**가 들어 있을 수 있습니다. 이슈를 신고하거나 파일을 공유하기 전에 개인정보를 지우세요.
- 스캔 캐시(`.pkl`)는 Python pickle 형식입니다. **출처를 모르는 캐시 파일은 열지 마세요.**

## 6. 지원

버그 수정, 기능 추가, 문의 응답은 **보장되지 않습니다.** 프로그램은 예고 없이 변경되거나 배포가 중단될 수 있습니다.

## 7. 제3자 상표와 기술

Windows, NTFS, BitLocker, exFAT 등은 각 소유자의 상표 또는 등록 상표이며, 이 프로그램은 해당 소유자와 제휴하거나 승인받은 것이 아닙니다. 이 프로그램은 공개된 자료와 분석을 바탕으로 독자적으로 작성되었습니다.

## 8. 준거법과 분쟁

이 문서와 프로그램 이용에 관한 사항은 `[대한민국 법]` 을 따릅니다. 분쟁이 생기면 `[관할 법원]` 을 관할 법원으로 합니다.

## 9. 동의

**이 프로그램을 내려받거나 실행하거나 사용하는 것은 이 문서와 LICENSE 의 내용에 동의하는 것으로 봅니다.** 동의하지 않으면 사용하지 마세요.

---

## English Summary

This software is provided "AS IS" under the MIT License, **without warranty of any kind**. Data recovery is never guaranteed. To the maximum extent permitted by law, the authors and contributors are not liable for any loss or damage — including data loss, hardware damage, or privacy exposure — arising from the use of or inability to use this software. Liability that cannot be excluded by law is not affected.

You are responsible for backing up your data, saving recovered files to a **different disk**, and using the tool only on storage you own or are authorized to access. Read-only design is not a guarantee of safety; if the data is important or the drive is failing, stop and consult a professional data recovery service. By downloading, running, or using this software you agree to these terms. If the MIT License text and this document differ, the MIT License text prevails.
