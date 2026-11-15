from machine import Pin, ADC, I2C
import dht
import ssd1306
import time
import math
import network
import json
import urequests
from umqtt.simple import MQTTClient
import ntptime
from email_diario import ResumoDiario
from sheets import enviar_planilha

# ---------- Limites dos alertas ----------
TEMP_ALTA = 35
TEMP_BAIXA = 5
UMID_BAIXA = 30
GAS_LIMITE = 60
IDT_PERIGO = 29

# ---------- Wi-Fi ----------
WIFI_SSID = "Wokwi-GUEST"
WIFI_SENHA = ""

# ---------- MQTT ----------
# broker público agora; pro HiveMQ Cloud: troca host, porta 8883, usuário, senha e USAR_TLS = True
MQTT_HOST = "broker.hivemq.com"
MQTT_PORTA = 1883
MQTT_USUARIO = None
MQTT_SENHA = None
USAR_TLS = False

MQTT_CLIENT_ID = "aeroclima-esp32-andre"
TOPICO_DADOS = "andre_quintino/aeroclima/dados"
TOPICO_PREVISAO = "andre_quintino/aeroclima/previsao"

INTERVALO_ENVIO = 5000   # ms
INTERVALO_SHEETS = 30000  # ms (1 linha na planilha a cada 30s)

# ---------- OpenWeatherMap ----------
OWM_CHAVE = "Sua chave"
OWM_CIDADE = "Mogi%20das%20Cruzes,BR"
INTERVALO_API = 60000    # ms (1 consulta por minuto)


PRESSAO_TESTE = None   # ex: 1013 pra testar sem a API

# ---------- Sensores ----------
sensor_dht = dht.DHT22(Pin(15))

ldr = ADC(Pin(34))
ldr.atten(ADC.ATTN_11DB)
ldr.width(ADC.WIDTH_12BIT)

mq2 = ADC(Pin(35))
mq2.atten(ADC.ATTN_11DB)
mq2.width(ADC.WIDTH_12BIT)

# ---------- Display OLED (I2C) ----------
i2c = I2C(0, scl=Pin(22), sda=Pin(21))
oled = ssd1306.SSD1306_I2C(128, 64, i2c)

# ---------- Botão ----------
botao = Pin(4, Pin.IN, Pin.PULL_UP)

TOTAL_TELAS = 5
tela_atual = 0
trocar_tela = False
ultimo_clique = 0


def clique(pin):
    global trocar_tela, ultimo_clique
    agora = time.ticks_ms()
    if time.ticks_diff(agora, ultimo_clique) > 250:  # debounce
        trocar_tela = True
        ultimo_clique = agora


botao.irq(trigger=Pin.IRQ_FALLING, handler=clique)


# ---------- Conexões ----------
def mensagem(linha1, linha2=""):
    oled.fill(0)
    oled.text(linha1, 0, 20)
    oled.text(linha2, 0, 34)
    oled.show()


def conectar_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if not wlan.isconnected():
        mensagem("Conectando", "Wi-Fi...")
        wlan.connect(WIFI_SSID, WIFI_SENHA)
        tentativas = 0
        while not wlan.isconnected() and tentativas < 40:
            time.sleep_ms(250)
            tentativas += 1

    if wlan.isconnected():
        print("Wi-Fi conectado:", wlan.ifconfig()[0])
        return True

    print("Falha no Wi-Fi")
    return False


def conectar_mqtt():
    mensagem("Conectando", "HiveMQ...")

    parametros = {
        "port": MQTT_PORTA,
        "user": MQTT_USUARIO,
        "password": MQTT_SENHA,
        "keepalive": 60,
    }
    if USAR_TLS:
        parametros["ssl"] = True
        parametros["ssl_params"] = {"server_hostname": MQTT_HOST}

    try:
        cliente = MQTTClient(MQTT_CLIENT_ID, MQTT_HOST, **parametros)
        cliente.connect()
        print("MQTT conectado")
        return cliente
    except Exception as e:
        print("Erro MQTT:", e)
        return None


def publicar(cliente, d, alertas, prev):
    pacote = dict(d)
    pacote["alertas"] = alertas
    cliente.publish(TOPICO_DADOS, json.dumps(pacote))
    cliente.publish(TOPICO_PREVISAO, json.dumps(prev))


def sincronizar_hora():
    mensagem("Sincronizando", "horario...")
    for _ in range(5):
        try:
            ntptime.settime()   # relógio em UTC; o fuso fica no email_diario.py
            print("Hora sincronizada (NTP)")
            return True
        except Exception as e:
            print("Falha no NTP:", e)
            time.sleep(1)
    return False


# ---------- Leituras ----------
def ler_dht():
    try:
        sensor_dht.measure()
        return sensor_dht.temperature(), sensor_dht.humidity()
    except OSError:
        return None, None


def ler_luz():
    bruto = ldr.read()
    tensao = bruto / 4095 * 3.3

    if tensao < 0.01:
        lux = 100000
    elif tensao >= 3.29:
        lux = 0
    else:
        resistencia = 2000 * tensao / (1 - tensao / 3.3)
        lux = (50 * 1000 * 10 ** 0.7 / resistencia) ** (1 / 0.7)

    if lux < 50:
        nivel = "Escuro"
    elif lux < 1000:
        nivel = "Pouca luz"
    elif lux < 10000:
        nivel = "Nublado"
    else:
        nivel = "Sol forte"

    return int(lux), nivel


def ler_ar():
    bruto = mq2.read()
    percentual = bruto / 4095 * 100

    if percentual < 30:
        qualidade = "Boa"
    elif percentual < GAS_LIMITE:
        qualidade = "Moderada"
    else:
        qualidade = "Ruim"

    return round(percentual, 1), qualidade


# ---------- Conforto térmico (Índice de Thom) ----------
def calcular_conforto(temp, umid):
    if temp is None:
        return None, "Sem dados"

    idt = temp - (0.55 - 0.0055 * umid) * (temp - 14.5)

    if idt < 15:
        classe = "Frio"
    elif idt < 21:
        classe = "Confortavel"
    elif idt < 24:
        classe = "Leve descon"
    elif idt < 27:
        classe = "Desconforto"
    elif idt < IDT_PERIGO:
        classe = "Muito desc."
    else:
        classe = "Perigo"

    return round(idt, 1), classe


# ---------- Alertas ----------
def verificar_alertas(d):
    alertas = []

    if d["temp"] is None:
        alertas.append("ERRO DHT22")
    else:
        if d["temp"] >= TEMP_ALTA:
            alertas.append("TEMP ALTA")
        if d["temp"] <= TEMP_BAIXA:
            alertas.append("TEMP BAIXA")
        if d["umid"] < UMID_BAIXA:
            alertas.append("UMID BAIXA")
        if d["idt"] >= IDT_PERIGO:
            alertas.append("CALOR PERIGOSO")

    if d["gas"] >= GAS_LIMITE:
        alertas.append("AR RUIM")

    return alertas


# ---------- OpenWeatherMap + previsão ----------
clima_api = None
pressao_atual = None
tendencia = 0.0
historico_pressao = []


def buscar_clima():
    url = (
        "http://api.openweathermap.org/data/2.5/weather?q={}"
        "&units=metric&lang=pt_br&appid={}"
    ).format(OWM_CIDADE, OWM_CHAVE)

    try:
        resposta = urequests.get(url)
        if resposta.status_code != 200:
            print("OpenWeather erro:", resposta.status_code)
            resposta.close()
            return None

        j = resposta.json()
        resposta.close()
        return {
            "pressao": j["main"]["pressure"],
            "temp_externa": j["main"]["temp"],
            "descricao": j["weather"][0]["description"],
        }
    except Exception as e:
        print("Falha OpenWeather:", e)
        return None


def calcular_tendencia(pressao):
    # janela das últimas 10 leituras (10 min com consulta a cada 1 min)
    historico_pressao.append(pressao)
    if len(historico_pressao) > 10:
        historico_pressao.pop(0)
    return round(historico_pressao[-1] - historico_pressao[0], 1)


def texto_tendencia(valor):
    if valor <= -1.0:
        return "Caindo"
    if valor >= 1.0:
        return "Subindo"
    return "Estavel"


def atualizar_pressao():
    global clima_api, pressao_atual, tendencia

    if PRESSAO_TESTE is not None:
        pressao_atual = PRESSAO_TESTE
        clima_api = {"temp_externa": None, "descricao": "modo teste"}
    else:
        resultado = buscar_clima()
        if resultado is None:
            return
        clima_api = resultado
        pressao_atual = resultado["pressao"]

    tendencia = calcular_tendencia(pressao_atual)
    print("Pressao:", pressao_atual, "hPa | Tendencia:", tendencia)


def prever_tempo(pressao, tend, temp, umid):
    if pressao is None or temp is None:
        return "Sem dados"

    # calor + umidade alta + pressão baixa ou caindo = tempestade de verão
    if temp >= 30 and umid >= 70 and (pressao < 1012 or tend < 0):
        return "Tempestade"

    # pressão caindo rápido ou baixa com ar saturado = chuva
    if tend <= -1.0 or (pressao < 1005 and umid >= 80):
        return "Chuva"

    if pressao < 1010 or umid >= 80:
        return "Nublado"

    # pressão alta e ar seco = tempo firme
    if pressao >= 1018 and umid < 65:
        return "Ensolarado" if temp >= 18 else "Ceu limpo"

    return "Parc. nublado"


def montar_previsao(d):
    return {
        "previsao": prever_tempo(pressao_atual, tendencia, d["temp"], d["umid"]),
        "pressao": pressao_atual,
        "tendencia": tendencia,
        "tendencia_texto": texto_tendencia(tendencia),
        "temp_sensor": d["temp"],
        "umid_sensor": d["umid"],
        "temp_externa": clima_api["temp_externa"] if clima_api else None,
        "clima_api": clima_api["descricao"] if clima_api else None,
    }


# ---------- Telas ----------
def barra_topo(titulo, alertas, piscar, indice):
    if alertas:
        msg = "! " + alertas[indice % len(alertas)]
        if piscar:
            oled.fill_rect(0, 0, 128, 10, 1)
            oled.text(msg, 0, 1, 0)
        else:
            oled.text(msg, 0, 1, 1)
    else:
        oled.text(titulo, 0, 1)
        oled.text("{}/{}".format(tela_atual + 1, TOTAL_TELAS), 104, 1)
    oled.hline(0, 11, 128, 1)


def barra_progresso(y, percentual):
    percentual = min(max(percentual, 0), 100)
    oled.rect(0, y, 128, 10, 1)
    oled.fill_rect(2, y + 2, int(124 * percentual / 100), 6, 1)


def tela_geral(d):
    if d["temp"] is None:
        oled.text("Erro no DHT22", 0, 14)
    else:
        oled.text("T:{:.1f}C U:{:.0f}%".format(d["temp"], d["umid"]), 0, 14)
        oled.text("Conf:" + d["conforto"], 0, 24)
        oled.text("IDT: {}".format(d["idt"]), 0, 34)
    oled.text("Luz: " + d["nivel_luz"], 0, 44)
    oled.text("Ar: " + d["qualidade"], 0, 54)


def tela_clima(d):
    if d["temp"] is None:
        oled.text("Erro no DHT22", 0, 30)
        return
    oled.text("Temp: {:.1f} C".format(d["temp"]), 0, 14)
    oled.text("Umid: {:.1f} %".format(d["umid"]), 0, 24)
    oled.text("IDT:  {}".format(d["idt"]), 0, 34)
    oled.text("Sensacao:", 0, 46)
    oled.text(d["conforto"], 0, 55)


def tela_luz(d):
    # escala logarítmica, já que o lux vai de 0 a 100000
    percentual = math.log10(d["lux"] + 1) / 5 * 100
    oled.text("Lux: {}".format(d["lux"]), 0, 14)
    barra_progresso(26, percentual)
    oled.text("Nivel:", 0, 42)
    oled.text(d["nivel_luz"], 0, 52)


def tela_ar(d):
    oled.text("Gas: {} %".format(d["gas"]), 0, 14)
    barra_progresso(26, d["gas"])
    oled.text("Qualidade:", 0, 42)
    oled.text(d["qualidade"], 0, 52)


def tela_previsao(d):
    oled.text("Previsao:", 0, 14)
    oled.text(previsao.get("previsao", "..."), 0, 24)
    if pressao_atual is None:
        oled.text("Aguardando API", 0, 40)
        return
    oled.text("P: {} hPa".format(pressao_atual), 0, 40)
    oled.text("Tend: " + texto_tendencia(tendencia), 0, 52)


TELAS = [
    ("GERAL", tela_geral),
    ("CLIMA", tela_clima),
    ("LUZ", tela_luz),
    ("AR", tela_ar),
    ("PREVISAO", tela_previsao),
]


def desenhar(d, alertas, piscar, indice):
    titulo, funcao = TELAS[tela_atual]
    oled.fill(0)
    barra_topo(titulo, alertas, piscar, indice)
    funcao(d)
    oled.show()


# ---------- Inicialização ----------
conectar_wifi()
sincronizar_hora()
cliente = conectar_mqtt()
resumo = ResumoDiario()

dados = {}
alertas = []
previsao = {}
piscar = False
indice_alerta = 0

ultima_leitura = time.ticks_ms() - 2000
ultimo_pisca = time.ticks_ms()
ultimo_envio = time.ticks_ms()
ultima_api = time.ticks_ms() - INTERVALO_API
ultimo_sheets = time.ticks_ms()

# ---------- Loop principal ----------
while True:
    agora = time.ticks_ms()

    # consulta da pressão na OpenWeatherMap a cada 60s
    if time.ticks_diff(agora, ultima_api) >= INTERVALO_API:
        ultima_api = agora
        atualizar_pressao()

    # leitura dos sensores a cada 2s
    if time.ticks_diff(agora, ultima_leitura) >= 2000:
        temp, umid = ler_dht()
        lux, nivel_luz = ler_luz()
        gas, qualidade = ler_ar()
        idt, conforto = calcular_conforto(temp, umid)

        dados = {
            "temp": temp, "umid": umid,
            "lux": lux, "nivel_luz": nivel_luz,
            "gas": gas, "qualidade": qualidade,
            "idt": idt, "conforto": conforto,
        }
        alertas = verificar_alertas(dados)
        previsao = montar_previsao(dados)
        ultima_leitura = agora

        print(dados, "| Alertas:", alertas, "| Previsao:", previsao["previsao"])

        # resumo do dia: soma da leitura e email na virada do dia
        resumo.adicionar(temp, umid)
        if resumo.pronto():
            mensagem("Enviando", "email diario...")
            if resumo.verificar():
                mensagem("Email enviado!", "")
                time.sleep(2)

    # envio pro HiveMQ a cada 5s
    if dados and time.ticks_diff(agora, ultimo_envio) >= INTERVALO_ENVIO:
        ultimo_envio = agora

        if cliente is None:
            conectar_wifi()
            cliente = conectar_mqtt()

        if cliente:
            try:
                publicar(cliente, dados, alertas, previsao)
                print("Enviado pro HiveMQ")
            except Exception as e:
                print("Falha no envio:", e)
                cliente = None

    # gravação no Google Sheets a cada 30s
    if dados and dados["temp"] is not None and time.ticks_diff(agora, ultimo_sheets) >= INTERVALO_SHEETS:
        ultimo_sheets = agora
        enviar_planilha(dados)

    # troca de tela pelo botão (redesenha na hora)
    if trocar_tela:
        trocar_tela = False
        tela_atual = (tela_atual + 1) % TOTAL_TELAS
        desenhar(dados, alertas, piscar, indice_alerta)

    # atualização do display a cada 0,5s (efeito de piscar)
    if time.ticks_diff(agora, ultimo_pisca) >= 500:
        piscar = not piscar
        if not piscar:
            indice_alerta += 1
        desenhar(dados, alertas, piscar, indice_alerta)
        ultimo_pisca = agora

    time.sleep_ms(50)