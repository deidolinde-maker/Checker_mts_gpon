pipeline {
    agent any

    parameters {
        choice(name: 'NETWORK_MODE', choices: ['auto', 'direct', 'proxy'], description: 'auto: direct → proxy №1 → proxy №2; direct: только direct; proxy: только proxy fallback.')
    }

    triggers {
        cron('TZ=Europe/Moscow\n0 6,12,17,20 * * *')
    }

    options {
        disableConcurrentBuilds()
        timeout(time: 30, unit: 'MINUTES')
    }

    environment {
        PYTHONUNBUFFERED = '1'
        REPORT_FILE = 'mts_gpon_report.json'
        CHECKER_CACHE_DIR = '/var/lib/jenkins/shared/Checker_mts_gpon'
        VENV_DIR = '/var/lib/jenkins/shared/Checker_mts_gpon/venv'
        PIP_CACHE_DIR = '/var/lib/jenkins/.cache/pip'
        PLAYWRIGHT_BROWSERS_PATH = '/var/lib/jenkins/.cache/ms-playwright'
    }

    stages {
        stage('Tests') {
            steps {
                script {
                    if (isUnix()) {
                        sh '''
                            set -e
                            mkdir -p "$CHECKER_CACHE_DIR" "$PIP_CACHE_DIR" "$PLAYWRIGHT_BROWSERS_PATH"
                            if [ ! -x "$VENV_DIR/bin/python" ]; then python3 -m venv "$VENV_DIR"; fi
                            "$VENV_DIR/bin/python" -m pip install -r requirements.txt
                            "$VENV_DIR/bin/python" -m playwright install chromium
                        '''
                    } else {
                        bat '''
                            if not exist "%VENV_DIR%\\Scripts\\python.exe" py -m venv "%VENV_DIR%"
                            "%VENV_DIR%\\Scripts\\python.exe" -m pip install -r requirements.txt
                            "%VENV_DIR%\\Scripts\\python.exe" -m playwright install chromium
                        '''
                    }
                }
                script {
                    def runChecker = {
                        if (isUnix()) {
                            sh '"$VENV_DIR/bin/python" -m pytest -q tests && "$VENV_DIR/bin/python" checker.py'
                        } else {
                            bat '"%VENV_DIR%\\Scripts\\python.exe" -m pytest -q tests && "%VENV_DIR%\\Scripts\\python.exe" checker.py'
                        }
                    }
                    if (params.NETWORK_MODE in ['auto', 'proxy']) {
                        withCredentials([
                            string(credentialsId: 'mts_gpon_proxy_1_url', variable: 'PROXY_1_URL'),
                            string(credentialsId: 'mts_gpon_proxy_2_url', variable: 'PROXY_2_URL')
                        ]) {
                            runChecker()
                        }
                    } else {
                        echo 'NETWORK_MODE=direct: proxy credentials не используются.'
                        runChecker()
                    }
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'mts_gpon_report.json', allowEmptyArchive: true
        }
        failure {
            script {
                withCredentials([
                    string(credentialsId: 'telegram_proxy_url', variable: 'TELEGRAM_PROXY_URL'),
                    string(credentialsId: 'telegram_proxy_auth_secret', variable: 'TELEGRAM_PROXY_AUTH_SECRET'),
                    string(credentialsId: 'tg_proxy_creds_survarius', variable: 'TELEGRAM_PROXY_CREDS')
                ]) {
                    if (isUnix()) {
                        sh '"$VENV_DIR/bin/python" notify.py || true'
                    } else {
                        bat '"%VENV_DIR%\\Scripts\\python.exe" notify.py'
                    }
                }
            }
        }
    }
}
