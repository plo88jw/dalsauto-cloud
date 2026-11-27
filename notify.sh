#!/usr/bin/env bash
# 알림 이슈("달스오토 알림")에 댓글을 달아 GitHub 모바일 앱 푸시를 보낸다. 사용: bash notify.sh <내용 파일>
# 댓글에 @계정 멘션이 있어 계정 주인에게 알림이 간다. 저장소가 공개라 이슈도 공개 — 조회 수치만 쓰고 쿠팡 클릭 등은 쓰지 않는다.
set -e
TITLE="달스오토 알림"
NUM=$(gh issue list --state open --search "\"$TITLE\" in:title" --json number --jq '.[0].number' || true)
if [ -z "$NUM" ]; then
  NUM=$(gh issue create --title "$TITLE" --body "@${OWNER} 시험 결과·발행 실패 알림이 이 이슈의 댓글로 올라옵니다. GitHub 앱에서 이 이슈를 구독(Subscribe)해 두세요." | grep -o '[0-9]*$')
fi
gh issue comment "$NUM" --body-file "$1"
