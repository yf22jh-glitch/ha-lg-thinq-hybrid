"""Register Web settings in the editable feature DB; never change account settings."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from contextlib import closing


def definitions():
    for model in ('CST_170004_WW', 'CST_570004_WW'):
        yield model, 'display.temperature_format', dict(domain='select', label_ko='HA · 온도 표시 단위·간격',
            source='ha-preference', options={x:x for x in ('0.5℃','1℃','1℉')},
            meaning='ThinQ Web app-side temperature display preference, stored in HA; no appliance packet',
            verification_status='web-source-and-unit-tests', web_browser_sync=False,
            web_source='GAM_SET11_TempUnit/main.js: setProductConfig when device temperatureUnit is unsupported')
        yield model, 'display.temperature_summary', dict(domain='sensor', label_ko='Local · 선택 단위 온도 표시',
            source='local-derived',meaning='Current/target Celsius from local state, formatted using this HA device preference; standard HA climate card keeps global units',
            verification_status='unit-tests;live-local-read-pending')
    for model in ('2REFO1DBN3K_U','3REK2G03VI230D_2'):
        yield model, 'night_mode.preview', dict(domain='button', label_ko='ThinQ · 야간 조명 미리보기',
            source='thinq-server', write_enabled=True, required_modes=['CUSTOM','SUNSET_RISE'],
            meaning='Preview saved interior brightness for 10 seconds; PREVIEW only, never SAVE. Request acknowledgement and unchanged saved settings do not measure physical light output.',
            verification_status='web-preview-wire-ack-saved-unchanged;ha-pilot-pending',
            web_source='GRM-20/GGM-20 antiGlareMode.setPreviewInsideLight; previewPeriodSec=10')
        yield model, 'night_mode.mode', dict(domain='select', label_ko='ThinQ · 야간 눈부심 방지 방식',
            source='thinq-server', options={'꺼짐':'OFF','일출·일몰':'SUNSET_RISE','사용자 지정':'CUSTOM'},
            meaning='Saved ThinQ night-mode tuple; switching to CUSTOM from OFF/sunset uses Web defaults 21:00–06:00',
            verification_status='ha-thinq-save-get-restore-confirmed',
            web_source='GRM-20/GGM-20 nightModes/antiGlareMode.js; service/fridge/night-mode')
        for field, label in [('start_time','시작 시각'), ('end_time','종료 시각')]:
            yield model, 'night_mode.'+field, dict(domain='time', label_ko='ThinQ · 야간 눈부심 방지 '+label,
                source='thinq-server', required_mode='CUSTOM',
                meaning='HH:MM in Asia/Seoul, CUSTOM only. Preserve brightness and the other clock; fresh saved GET confirms once-only SAVE',
                verification_status='ha-thinq-save-get-restore-confirmed',
                web_source='GRM-20/GGM-20 nightModes/antiGlareMode.js; service/fridge/night-mode')
        yield model, 'food.recommended_period', dict(domain='select',
            label_ko='ThinQ · 추천 보관 기한',source='thinq-server',options={'꺼짐':'off','켜짐':'on'},
            meaning='Use LG recommended storage period when registering food; not push notification permission',
            verification_status=('live-toggle-and-restore-confirmed' if model=='2REFO1DBN3K_U'
                                 else 'live-D-displays-off;write-protocol-tested;live-toggle-not-performed'),
            web_source='GRM-20/GGM-20 gear menu storedPeriod -> foodManager.foodRecommend -> SS11_11',
            value_mapping={'Y':'on','N':'off','D':'off (Web comparison is exactly Y)'})
    yield 'ST_R_ETH01Y_', 'smart_pairing', dict(domain='select',label_ko='ThinQ · 스마트 페어링',
        source='thinq-server',options={},write_enabled=True,
        execution_condition=dict(states=['INITIAL'],required=dict(online=True,child_lock='CHILDLOCK_OFF',error='ERROR_NO'),
            power_off_state='POWEROFF',power_off_support_key='settingFuncEnableInPowerOff',power_off_content='203-3'),
        meaning='LG account washer-to-styler pairing, not power/start or device removal',
        verification_status='live-current-pair-and-conditions-read;write-protocol-tested;live-pairing-unchanged',
        web_source='TCL/JUS ST smartPairing.js + ST/libs/util/util.js isAvailable')


def install(path, apply=False):
    path=Path(path).absolute()
    with closing(sqlite3.connect(path.as_uri()+('?mode=rw' if apply else '?mode=ro'),uri=True)) as c:
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_settings'").fetchone()
        existing={(r[0],r[1]) for r in c.execute('SELECT model_id,feature_id FROM app_settings')} if exists else set()
        rows=[(m,f,json.dumps(d,ensure_ascii=False,sort_keys=True)) for m,f,d in definitions() if (m,f) not in existing]
        result=dict(new_definitions=len(rows),appliance_commands=0,account_writes=0)
        if not apply: return dict(result,status='ready')
        if not rows: return dict(result,status='already-installed')
        backup=Path(tempfile.mkdtemp(prefix='app-settings-before-',dir=path.parent))/'features.sqlite3'
        with closing(sqlite3.connect(backup)) as dest: c.backup(dest)
        os.chmod(backup,0o600)
        with c:
            c.execute('CREATE TABLE IF NOT EXISTS app_settings(model_id TEXT NOT NULL,feature_id TEXT NOT NULL,'
                'definition_json TEXT NOT NULL CHECK(json_valid(definition_json)),enabled INTEGER NOT NULL DEFAULT 1,'
                'delete_requested INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(model_id,feature_id))')
            c.executemany('INSERT OR IGNORE INTO app_settings(model_id,feature_id,definition_json) VALUES(?,?,?)',rows)
        return dict(result,status='installed',backup=str(backup))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(install(args.database,args.apply),ensure_ascii=False))
