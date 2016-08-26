import binascii
import json
import logging
import sys

import hashlib
import requests
from bitcoin.signmessage import BitcoinMessage, VerifyMessage
from cert_schema.schema_tools import schema_validator
from merkleproof import utils
from merkleproof.MerkleTree import sha256
from pyld import jsonld

unhexlify = binascii.unhexlify
hexlify = binascii.hexlify
if sys.version > '3':
    unhexlify = lambda h: binascii.unhexlify(h.encode('utf8'))
    hexlify = lambda b: binascii.hexlify(b).decode('utf8')


class Error(Exception):
    """Base class for exceptions in this module"""
    pass


class InvalidTransactionError(Error):
    pass


def lookup_transaction(transaction_id):
    r = requests.get(
        "https://blockchain.info/rawtx/%s?cors=true" %
        transaction_id)
    if r.status_code != 200:
        logging.error(
            'Error looking up by transaction_id=%s, status_code=%d',
            transaction_id,
            r.status_code)
        return None
    else:
        return r.json()



def verify_v2(signed_local_json):
    verify_response = []
    verified = False


    valid_proof = utils.validate_proof(signed_local_json['receipt']['proof'],
                        signed_local_json['receipt']['targetHash'],
                        signed_local_json['receipt']['merkleRoot'], sha256)
    print(valid_proof)

    # TODO: validation should now happen through normalized form



    transaction_id = signed_local_json['receipt']['anchors'][0]['sourceId']
    transaction_data = lookup_transaction(transaction_id)
    if not transaction_data:
        verify_response.append(('Looking up by transaction_id', False))
        verify_response.append(("Verified", False))
        return verify_response

    # transaction was found
    verify_response.append(
        ("Computing SHA256 digest of local certificate", "DONE"))
    verify_response.append(("Fetching hash in OP_RETURN field", "DONE"))

    signer_url = signed_local_json['document']['certificate']['issuer']['id']
    uid = signed_local_json['document']['assertion']['uid']
    signature = signed_local_json['document']['signature']

    # compare hashes
    target_hash = signed_local_json['receipt']['targetHash']
    compare_hash_result = compare_hashes_v2(signed_local_json, target_hash)
    verify_response.append(
        ("Comparing local and blockchain hashes", compare_hash_result))

    # check merkle root
    merkle_root = signed_local_json['receipt']['merkleRoot']
    remote_hash = get_hash_from_bc_op(transaction_data)
    compare_hash_result = _compare_hashes(merkle_root, remote_hash)

    keys = get_issuer_keys(signer_url)
    signing_key = keys['issuer_key'][0]['key']
    revocation_address = keys['revocation_key'][0]['key']

    # check author
    author_verified = check_issuer_signature(signing_key, uid, signature)
    verify_response.append(("Checking signature", author_verified))

    # check if it's been revoked by the issuer
    not_revoked = check_revocation(transaction_data, revocation_address)
    verify_response.append(("Checking not revoked by issuer", not_revoked))

    if compare_hash_result and author_verified and not_revoked:
        verified = True
    verify_response.append(("Verified", verified))
    return verify_response


def verify_v1(transaction_id, signed_local_json, signed_local_file):
    verify_response = []
    verified = False

    # TODO: add some initial validation before looking up transaction

    transaction_info = lookup_transaction(transaction_id)

    if not transaction_info:
        verify_response.append(('Looking up by transaction_id', False))
        verify_response.append(("Verified", False))
        return verify_response

    # transaction was found
    verify_response.append(
        ("Computing SHA256 digest of local certificate", "DONE"))
    verify_response.append(("Fetching hash in OP_RETURN field", "DONE"))

    signer_url = signed_local_json['document']['certificate']['issuer']['id']
    uid = signed_local_json['document']['assertion']['uid']
    signature = signed_local_json['document']['signature']

    # compare hashes
    compare_hash_result = compare_hashes(signed_local_file, transaction_info)
    verify_response.append(
        ("Comparing local and blockchain hashes", compare_hash_result))

    keys = get_issuer_keys(signer_url)
    signing_key = keys['issuer_key'][0]['key']
    revocation_address = keys['revocation_key'][0]['key']

    # check author
    author_verified = check_issuer_signature(signing_key, uid, signature)
    verify_response.append(("Checking signature", author_verified))

    # check if it's been revoked by the issuer
    not_revoked = check_revocation(transaction_info, revocation_address)
    verify_response.append(("Checking not revoked by issuer", not_revoked))

    if compare_hash_result and author_verified and not_revoked:
        verified = True
    verify_response.append(("Verified", verified))
    return verify_response


def check_issuer_signature(signing_key, uid, signature):
    if signing_key is None or uid is None or signature is None:
        return False

    message = BitcoinMessage(uid)
    return VerifyMessage(signing_key, message, signature)


def compare_hashes_v2(signed_local_json, target_hash):
    local_hash = compute_v2_hash(signed_local_json)
    compare_hash_result = _compare_hashes(local_hash, target_hash)
    return compare_hash_result


def compare_hashes(signed_local_file, transaction_info):
    local_hash = compute_v1_hash(signed_local_file)
    remote_hash = get_hash_from_bc_op(transaction_info)
    compare_hash_result = _compare_hashes(local_hash, remote_hash)
    return compare_hash_result


def get_issuer_keys(signer_url):
    r = requests.get(signer_url)
    remote_json = None
    if r.status_code != 200:
        logging.error(
            'Error looking up issuer keys at url=%s, status_code=%d',
            signer_url, r.status_code)
    else:
        remote_json = r.json()
        logging.info(
            'Found issuer keys at url=%s', signer_url)
    return remote_json


def get_hash_from_bc_op(tx_json):
    tx_outs = tx_json['out']
    op_tx = None
    for o in tx_outs:
        if int(o.get('value', 1)) == 0:
            op_tx = o
    if not op_tx:
        raise InvalidTransactionError('transaction is missing op_return ')
    return op_tx['script']


def check_revocation(tx_json, revoke_address):
    tx_outs = tx_json['out']
    for o in tx_outs:
        if o.get('addr') == revoke_address and o.get('spent') is False:
            return True
    return False


def compute_v1_hash(doc):
    doc_bytes = doc
    if not isinstance(doc, (bytes, bytearray)):
        doc_bytes = doc.encode('utf-8')
    return hashlib.sha256(doc_bytes).hexdigest()


def compute_v2_hash(signed_local_json):
    normalized = jsonld.normalize(cert_json, {'algorithm': 'URDNA2015', 'format': 'application/nquads'})
    hashed = sha256(normalized)
    return hashed

def do_hash_certificate(self, certificate):

    return hashed

def _compare_hashes(hash1, hash2):
    if hash1 in hash2 or hash1 == hash2:
        return True
    return False


if __name__ == "__main__":

    # TODO: validate before running

    #schema_updater.update('sample_data/1.1.0/sample_signed_cert-1.1.0.json',
    #                      'sample_data/1.2.0/sample_signed_cert-1.2.0.json-new')

    with open('sample_data/1.2.0/sample_signed_cert-1.2.0.json') as cert_file:
        cert_json = json.load(cert_file)
        schema_validator.validate_v1_2_0(cert_json)
        verify_v2(cert_json, cert_file.read())


    #with open('sample_data/1.1.0/sample_signed_cert-1.1.0.json', 'rb') as cert_file:
    #    result = verify_v1('d5df311055bf0fe656b9d6fa19aad15c915b47303e06677b812773c37050e35d', cert_json, cert_file.read())
    #    print(result)