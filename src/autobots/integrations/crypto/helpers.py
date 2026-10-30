from src.autobots.integrations.crypto.encoder_decoder import decrypt, encrypt, is_encrypted


def encrypt_if_needed(data: dict) -> dict:
    if is_encrypted(data):
        return data
    return encrypt(data)


def decrypt_if_needed(data: dict) -> dict:
    if is_encrypted(data):
        return decrypt(data)
    return data
