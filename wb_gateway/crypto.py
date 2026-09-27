from Crypto.Cipher import AES

class AESGCM:
    # Preserve the existing ciphertext followed by a 16-byte authentication tag.
    def __init__(self, key):
        if len(key) not in (16, 24, 32):
            raise ValueError('AESGCM key must be 128, 192, or 256 bits')
        self.key = bytes(key)

    def encrypt(self, nonce, data, associated_data):
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(associated_data or b'')
        ciphertext, tag = cipher.encrypt_and_digest(data)
        return ciphertext + tag

    def decrypt(self, nonce, data, associated_data):
        if len(data) < 16:
            raise ValueError('Invalid authenticated ciphertext')
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(associated_data or b'')
        return cipher.decrypt_and_verify(data[:-16], data[-16:])
