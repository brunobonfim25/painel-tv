"""Remove o fundo de uma imagem: lê os bytes da imagem na entrada padrão
e escreve o PNG com transparência na saída padrão.

Roda como processo separado, chamado por remover_fundo_bytes() em
app.py. Assim o modelo de IA (rembg + onnxruntime, centenas de MB de
RAM) só existe enquanto há uma foto sendo processada, e toda essa
memória volta ao sistema quando o processo termina. Carregado dentro
dos workers do gunicorn, o modelo ficava residente o tempo todo, uma
cópia por worker (~0,75 GB cada), mesmo sem nenhuma foto para recortar.
"""
import sys


def main():
    dados = sys.stdin.buffer.read()
    if not dados:
        sys.exit(2)
    from rembg import remove, new_session
    resultado = remove(dados, session=new_session("u2netp"))
    sys.stdout.buffer.write(resultado)


if __name__ == "__main__":
    main()
