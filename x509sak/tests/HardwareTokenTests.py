#	x509sak - The X.509 Swiss Army Knife white-hat certificate toolkit
#	Copyright (C) 2018-2018 Johannes Bauer
#
#	This file is part of x509sak.
#
#	x509sak is free software; you can redistribute it and/or modify
#	it under the terms of the GNU General Public License as published by
#	the Free Software Foundation; this program is ONLY licensed under
#	version 3 of the License, later versions are explicitly excluded.
#
#	x509sak is distributed in the hope that it will be useful,
#	but WITHOUT ANY WARRANTY; without even the implied warranty of
#	MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#	GNU General Public License for more details.
#
#	You should have received a copy of the GNU General Public License
#	along with x509sak; if not, write to the Free Software
#	Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#
#	Johannes Bauer <JohannesBauer@gmx.de>

import os
import unittest
import pkgutil
import tempfile
import urllib
from x509sak.SubprocessExecutor import SubprocessExecutor
from x509sak.Tools import PathTools
from x509sak.WorkDir import WorkDir
from x509sak.Exceptions import CmdExecutionFailedException

class SoftHSMInstance(object):
	_SO_SEARCH_PATH = "/usr/local/lib/softhsm:/usr/lib/softhsm:/usr/local/lib:/usr/lib:/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu/openssl-1.0.2/engines:/usr/lib/x86_64-linux-gnu/engines-1.1"

	@property
	def so_search_path(self):
		return self._SO_SEARCH_PATH

	@property
	def env(self):
		return self._env

	@property
	def module_so(self):
		return self._module_so

	def __enter__(self):
		self._softhsm_config_file = tempfile.NamedTemporaryFile(mode = "w", prefix = "softhsm_", suffix = ".conf")
		self._softhsm_storage_dir = tempfile.TemporaryDirectory(prefix = "objects_")

		try:
			self._module_so = PathTools.find(self._SO_SEARCH_PATH, "libsofthsm2.so")
			if self._module_so is None:
				raise Exception("libsofthsm2.so cannot be found anywhere in %s" % (self._SO_SEARCH_PATH))

			print("directories.tokendir = %s" % (self._softhsm_storage_dir.name), file = self._softhsm_config_file)
			print("directories.backend = file", file = self._softhsm_config_file)
			print("log.level = INFO", file = self._softhsm_config_file)
			self._softhsm_config_file.flush()
			self._env = { "SOFTHSM2_CONF": self._softhsm_config_file.name }
			SubprocessExecutor.run([ "softhsm2-util", "--init-token", "--slot", "0", "--label", "TestToken", "--so-pin", "3537363231383830", "--pin", "648219" ], env = self._env)

			return self
		except:
			print("Fatal error: SoftHSM initialization failed.")
			self.__cleanup()
			raise

	def keygen(self, key_id, key_label, key_spec):
		SubprocessExecutor.run([ "pkcs11-tool", "--module", self.module_so, "--login", "--pin", "648219", "--keypairgen", "--key-type", key_spec, "--id", "%x" % (key_id), "--label", key_label ], env = self._env)
		return "pkcs11:object=%s;type=private;pin-value=648219;token=TestToken" % (urllib.parse.quote(key_label))

	def list_objects(self):
		(success, output) = SubprocessExecutor.run([ "pkcs11-tool", "--module", self.module_so, "--login", "--pin", "648219", "--list-objects" ], env = self._env, return_stdout = True)
		print(output.decode())

	def get_pubkey_der(self, key_id):
		(success, output) = SubprocessExecutor.run([ "pkcs11-tool", "--module", self.module_so, "--login", "--pin", "648219", "--read-object", "--type", "pubkey", "--id", "%x" % (key_id) ], env = self._env, return_stdout = True, discard_stderr = True)
		return output

	def get_ecc_pubkey_text(self, key_id):
		pubkey = self.get_pubkey_der(key_id)
		(success, output) = SubprocessExecutor.run([ "openssl", "ec", "-inform", "der", "-pubin", "-text", "-noout" ], stdin = pubkey, return_stdout = True, discard_stderr = True)
		output = output.decode()
		return output

	def __cleanup(self):
		self._softhsm_storage_dir.cleanup()
		self._softhsm_config_file.close()

	def __exit__(self, *args):
		self.__cleanup()

class HardwareTokenTests(unittest.TestCase):
	def setUp(self):
		self._x509sak = os.path.realpath("x509sak.py")

	def test_create_ca(self):
		with SoftHSMInstance() as hsm, tempfile.TemporaryDirectory() as tempdir, WorkDir(tempdir):
			key_name = hsm.keygen(key_id = 12345, key_label = "my secure key", key_spec = "EC:secp256r1")
			pubkey = hsm.get_ecc_pubkey_text(key_id = 12345)
			pubkey = pubkey.split("\n")
			pubkey_start = pubkey[2]
			self.assertTrue(pubkey_start.startswith("    04:"))
			pubkey_start = pubkey_start[4:].encode("ascii")

			# With unknown key object name, it fails
			with self.assertRaises(CmdExecutionFailedException):
				SubprocessExecutor.run([ self._x509sak, "createca", "-f", "-s", "/CN=Root CA with key in HSM", "--pkcs11-so-search", hsm.so_search_path, "--pkcs11-module", "libsofthsm2.so", "--hardware-key", key_name.replace("secure", "UNKNOWN"), "root_ca" ], env = hsm.env, on_failure = "exception-nopause")

			# With unknown token name, it fails
			with self.assertRaises(CmdExecutionFailedException):
				SubprocessExecutor.run([ self._x509sak, "createca", "-f", "-s", "/CN=Root CA with key in HSM", "--pkcs11-so-search", hsm.so_search_path, "--pkcs11-module", "libsofthsm2.so", "--hardware-key", key_name.replace("Token", "UNKNOWN"), "root_ca" ], env = hsm.env, on_failure = "exception-nopause")

			# Create root certificate with key in SoftHSM
			SubprocessExecutor.run([ self._x509sak, "createca", "-f", "-s", "/CN=Root CA with key in HSM", "--pkcs11-so-search", hsm.so_search_path, "--pkcs11-module", "libsofthsm2.so", "--hardware-key", key_name, "root_ca" ], env = hsm.env)

			# Check that it's validly self-signed
			SubprocessExecutor.run([ "openssl", "verify", "-check_ss_sig", "-CAfile", "root_ca/CA.crt", "root_ca/CA.crt" ])

			# Check that the public key on the smart card appears inside the certificate
			(success, cert) = SubprocessExecutor.run([ "openssl", "x509", "-noout", "-text", "-in", "root_ca/CA.crt" ], return_stdout = True)
			self.assertIn(pubkey_start, cert)


	def test_create_certificate(self):
		with SoftHSMInstance() as hsm, tempfile.TemporaryDirectory() as tempdir, WorkDir(tempdir):
			key_name = hsm.keygen(key_id = 1, key_label = "CA_key", key_spec = "EC:secp256r1")

			# Create root certificate with key in SoftHSM
			SubprocessExecutor.run([ self._x509sak, "createca", "-s", "/CN=Root CA with key in HSM", "--pkcs11-so-search", hsm.so_search_path, "--pkcs11-module", "libsofthsm2.so", "--hardware-key", key_name, "root_ca" ], env = hsm.env)

			# Then create child certificate with that CA, but have child key in software
			SubprocessExecutor.run([ self._x509sak, "createcsr", "-s", "/CN=Child Cert", "-t", "tls-client", "-c", "root_ca", "client.key", "client.crt" ], env = hsm.env)

			# Verify the child certificate is valid
			SubprocessExecutor.run([ "openssl", "verify", "-check_ss_sig", "-CAfile", "root_ca/CA.crt", "client.crt" ])

	def test_sign_csr(self):
		with SoftHSMInstance() as hsm, tempfile.TemporaryDirectory() as tempdir, WorkDir(tempdir):
			key_name = hsm.keygen(key_id = 1, key_label = "CA_key", key_spec = "EC:secp256r1")

			# Create root certificate with key in SoftHSM
			SubprocessExecutor.run([ self._x509sak, "createca", "-s", "/CN=Root CA with key in HSM", "--pkcs11-so-search", hsm.so_search_path, "--pkcs11-module", "libsofthsm2.so", "--hardware-key", key_name, "root_ca" ], env = hsm.env)

			# Then create child CSR
			SubprocessExecutor.run([ self._x509sak, "createcsr", "client.key", "client.csr" ], env = hsm.env)

			# Finally, sign the child certificate
			SubprocessExecutor.run([ self._x509sak, "signcsr", "-s", "/CN=Child Cert", "-t", "tls-client", "root_ca", "client.csr", "client.crt" ], env = hsm.env)

			# Verify the child certificate is valid
			SubprocessExecutor.run([ "openssl", "verify", "-check_ss_sig", "-CAfile", "root_ca/CA.crt", "client.crt" ])