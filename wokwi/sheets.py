# sheets.py
# Gravação das leituras no Google Sheets direto pelo ESP32,
# via requisição HTTPS para o Apps Script (App da Web) da planilha.

import socket
import gc

try:
    import ssl
except ImportError:
    import ussl as ssl

SHEETS_HOST = "script.google.com"
SHEETS_CAMINHO = "/macros/s/ID_DA_IMPLANTACAO/exec"


def _codificar(valor):
    # codificação de URL (espaços, acentos e símbolos viram %XX)
    seguros = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    saida = ""
    for b in str(valor).encode():
        if b in seguros:
            saida += chr(b)
        else:
            saida += "%{:02X}".format(b)
    return saida


def _montar_query(d):
    campos = {
        "temp": d["temp"],
        "umid": d["umid"],
        "lux": d["lux"],
        "gas": d["gas"],
        "qualidade": d["qualidade"],
        "idt": d["idt"],
        "conforto": d["conforto"],
    }
    return "&".join("{}={}".format(k, _codificar(v)) for k, v in campos.items())


def enviar_planilha(d):
    gc.collect()  # TLS precisa de RAM livre
    sock = None
    try:
        endereco = socket.getaddrinfo(SHEETS_HOST, 443)[0][-1]
        bruto = socket.socket()
        bruto.settimeout(15)
        bruto.connect(endereco)
        sock = ssl.wrap_socket(bruto, server_hostname=SHEETS_HOST)

        requisicao = (
            "GET {}?{} HTTP/1.1\r\n"
            "Host: {}\r\n"
            "User-Agent: AeroClima-ESP32\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).format(SHEETS_CAMINHO, _montar_query(d), SHEETS_HOST)
        sock.write(requisicao.encode())

        # só a linha de status importa: o Apps Script grava a linha e
        # responde 302 (redirecionamento pro resultado), então 302 = sucesso
        status = sock.readline()
        codigo = int(status.split()[1])
        if codigo in (200, 302):
            print("[SHEETS] Linha gravada na planilha")
            return True
        print("[SHEETS] Resposta inesperada:", status)
        return False
    except Exception as e:
        print("[SHEETS] Falha ao gravar:", e)
        return False
    finally:
        if sock:
            sock.close()
        gc.collect()
