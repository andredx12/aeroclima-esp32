# email_diario.py
# Resumo diário (temperatura/umidade) enviado por email via SMTP do Gmail,
# direto do ESP32, sem Node-RED ou script externo.

import socket
import time
import gc
import ubinascii

try:
    import ssl
except ImportError:
    import ussl as ssl

# ===== Configuração =====
SMTP_HOST = "smtp.gmail.com"
SMTP_PORTA = 465                      # SSL direto
GMAIL_USUARIO = "seu_email@gmail.com"
GMAIL_SENHA_APP = "SENHA_DE_APP_AQUI"     # senha de app de 16 letras (não é a senha normal)
DESTINATARIOS = ["destinatario@exemplo.com", "seu_email@gmail.com"]

FUSO_HORARIO = -3                     # horário de Brasília

# Modo demonstração: envia o resumo a cada N segundos (para gravar o vídeo).
# Com None, o envio acontece só na virada do dia.
INTERVALO_DEMO = None   # 60 no vídeo de demonstração


def _b64(texto):
    if isinstance(texto, str):
        texto = texto.encode()
    return ubinascii.b2a_base64(texto).strip()


def _hora_local():
    return time.localtime(time.time() + FUSO_HORARIO * 3600)


# ===== Protocolo SMTP =====
def _resposta(sock, esperado):
    linha = sock.readline()
    # respostas multilinha usam "250-" e terminam em "250 "
    while linha and linha[3:4] == b"-":
        linha = sock.readline()
    codigo = int(linha[:3])
    if codigo != esperado:
        raise OSError("SMTP esperava {} e recebeu: {}".format(esperado, linha))
    return linha


def _comando(sock, cmd, esperado):
    if isinstance(cmd, str):
        cmd = cmd.encode()
    sock.write(cmd + b"\r\n")
    return _resposta(sock, esperado)


def enviar_email(assunto, corpo):
    gc.collect()  # TLS no ESP32 precisa de bastante RAM livre
    endereco = socket.getaddrinfo(SMTP_HOST, SMTP_PORTA)[0][-1]
    bruto = socket.socket()
    bruto.settimeout(15)
    bruto.connect(endereco)
    sock = ssl.wrap_socket(bruto, server_hostname=SMTP_HOST)

    try:
        _resposta(sock, 220)                              # saudação do servidor
        _comando(sock, "EHLO esp32", 250)

        # Autenticação AUTH LOGIN (usuário e senha em base64)
        _comando(sock, "AUTH LOGIN", 334)
        _comando(sock, _b64(GMAIL_USUARIO), 334)
        _comando(sock, _b64(GMAIL_SENHA_APP.replace(" ", "")), 235)

        # Envelope
        _comando(sock, "MAIL FROM:<{}>".format(GMAIL_USUARIO), 250)
        for dest in DESTINATARIOS:
            _comando(sock, "RCPT TO:<{}>".format(dest), 250)

        # Montagem da mensagem
        _comando(sock, "DATA", 354)
        assunto_codificado = "=?UTF-8?B?{}?=".format(_b64(assunto).decode())
        cabecalho = (
            "From: AeroClima ESP32 <{}>\r\n"
            "To: {}\r\n"
            "Subject: {}\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=UTF-8\r\n"
            "Content-Transfer-Encoding: 8bit\r\n"
            "\r\n"
        ).format(GMAIL_USUARIO, ", ".join(DESTINATARIOS), assunto_codificado)

        sock.write(cabecalho.encode())
        sock.write(corpo.replace("\n", "\r\n").encode())
        _comando(sock, "\r\n.", 250)                       # fim do DATA

        _comando(sock, "QUIT", 221)
        print("[EMAIL] Resumo enviado com sucesso")
        return True
    finally:
        sock.close()
        gc.collect()


# ===== Acumulador do dia =====
class ResumoDiario:
    def __init__(self):
        self._zerar()
        self.dia = _hora_local()[:3]
        self.inicio_demo = time.time()

    def _zerar(self):
        self.soma_temp = 0.0
        self.soma_umid = 0.0
        self.leituras = 0

    def adicionar(self, temp, umid):
        if temp is None or umid is None:
            return
        self.soma_temp += temp
        self.soma_umid += umid
        self.leituras += 1

    def _montar(self, dia):
        data = "{:02d}/{:02d}/{:04d}".format(dia[2], dia[1], dia[0])
        assunto = "Relatório diário AeroClima - {}".format(data)
        if self.leituras:
            media_t = self.soma_temp / self.leituras
            media_u = self.soma_umid / self.leituras
        else:
            media_t = media_u = 0.0
        corpo = (
            "Relatório diário da estação AeroClima (ESP32)\n"
            "\n"
            "Data do relatório: {}\n"
            "Média da temperatura: {:.2f} °C\n"
            "Média da umidade: {:.2f} %\n"
            "Total de leituras registradas: {}\n"
            "\n"
            "Email enviado automaticamente pelo ESP32 via SMTP do Gmail."
        ).format(data, media_t, media_u, self.leituras)
        return assunto, corpo

    def pronto(self):
        """Hora de enviar? (virada do dia ou intervalo do modo demo)"""
        if self.leituras == 0:
            return False
        virou_dia = _hora_local()[:3] != self.dia
        demo = INTERVALO_DEMO and (time.time() - self.inicio_demo >= INTERVALO_DEMO)
        return bool(virou_dia or demo)

    def verificar(self):
        """Chamar a cada volta do loop principal."""
        if not self.pronto():
            return False

        hoje = _hora_local()[:3]
        assunto, corpo = self._montar(self.dia)
        try:
            enviar_email(assunto, corpo)
        except Exception as e:
            print("[EMAIL] Falha no envio:", e)
            self.inicio_demo = time.time()  # nova tentativa no próximo ciclo
            return False

        self._zerar()
        self.dia = hoje
        self.inicio_demo = time.time()
        return True
