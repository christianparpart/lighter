import unittest
import os
import urllib2
import shutil
import tempfile
from mock import patch, Mock
import lighter.main as lighter
from lighter.util import jsonRequest

PROFILE_2 = 'src/resources/yaml/myprofile2.yml'

PROFILE_1 = 'src/resources/yaml/myprofile1.yml'


class DeployTest(unittest.TestCase):
    def setUp(self):
        self._called = False

    def testParseService(self):
        service = lighter.parse_service('src/resources/yaml/staging/myservice.yml', profiles=[PROFILE_1, PROFILE_2])
        self.assertEquals(service.document['hipchat']['token'], 'abc123')
        self.assertEquals(sorted(service.document['hipchat']['rooms']), ['123', '456', '456', '789'])
        self.assertEquals(service.environment, 'staging')

        self.assertEquals(service.config['id'], '/myproduct/myservice')
        self.assertEquals(service.config['env']['DATABASE'], 'database:3306')
        self.assertEquals(service.config['env']['rabbitmq'], 'amqp://myserver:15672')
        self.assertEquals(service.config['cpus'], 1)
        self.assertEquals(service.config['instances'], 3)
        self.assertEquals(service.config['env']['SERVICE_VERSION'], '1.0.0')
        self.assertEquals(service.config['env']['SERVICE_BUILD'], '1.0.0')
        self.assertEquals(service.config['env']['MY_ESCAPED_VAR'], '%{id}')

        self.assertEquals(service.config['container']['docker']['parameters'][0]['key'], 'label')
        self.assertEquals(service.config['container']['docker']['parameters'][0]['value'], 'com.meltwater.lighter.appid='+service.config['id'])

        # Check that zero are translated correctly
        self.assertEquals(service.config['upgradeStrategy']['minimumHealthCapacity'], 0.0)
        self.assertEquals(service.config['upgradeStrategy']['maximumOverCapacity'], 0.0)

    def testParseNonDockerService(self):
        service = lighter.parse_service('src/resources/yaml/staging/myservice-non-docker.yml')
        self.assertEquals(service.config['id'], '/myservice/hello-play')

        self.assertEquals(service.config['cpus'], 1)
        self.assertEquals(service.config['instances'], 1)
        self.assertFalse('container' in service.config)

    def testParseEnvVariable(self):
        os.environ['VERSION'] = '1.0.0'
        os.environ['DATABASE'] = 'hostname:3306'
        os.environ['RABBITMQ_URL'] = 'amqp://hostname:5672/%2F'
        service = lighter.parse_service('src/resources/yaml/staging/myservice-env-variable.yml')
        self.assertEquals(service.config['container']['docker']['image'], 'meltwater/myservice:1.0.0')
        self.assertEquals(service.config['env']['DATABASE'], 'hostname:3306')

        service = lighter.parse_service('src/resources/yaml/staging/myservice-env-maven.yml')
        self.assertEquals(service.config['container']['docker']['image'], 'meltwater/myservice:1.0.0')
        self.assertEquals(service.config['env']['DATABASE'], 'hostname:3306')
        self.assertEquals(service.config['env']['RABBITMQ_URL'], 'amqp://hostname:5672/%2F')

    def testParseClassifier(self):
        service = lighter.parse_service('src/resources/yaml/staging/myservice-classifier.yml')
        self.assertEquals(service.config['env']['isclassifier'], 'marathon')
        self.assertEquals(service.config['env']['SERVICE_VERSION'], '1.0.0')
        self.assertEquals(service.config['env']['SERVICE_BUILD'], '1.0.0-marathon')

    def testParseRecursiveVariable(self):
        service = lighter.parse_service('src/resources/yaml/staging/myservice.yml')
        self.assertEquals(service.config['env']['BVAR'], '123')
        self.assertEquals(service.config['env']['CVAR'], '123')

    def testParseSnapshot(self):
        service = lighter.parse_service('src/resources/yaml/staging/myservice-snapshot.yml')
        self.assertEquals(service.config['env']['SERVICE_VERSION'], '1.1.1-SNAPSHOT')
        self.assertEquals(service.config['env']['SERVICE_BUILD'], '1.1.1-20151105011659')

    def testParseUniqueSnapshot(self):
        service = lighter.parse_service('src/resources/yaml/staging/myservice-unique-snapshot.yml')
        self.assertEquals(service.config['env']['SERVICE_VERSION'], '1.1.1-SNAPSHOT')
        self.assertEquals(service.config['env']['SERVICE_BUILD'], '1.1.1-20151102.035053-8-marathon')

    def testParseListOverride(self):
        # servicePort=1234 in json, no override in yaml
        service = lighter.parse_service('src/resources/yaml/staging/myservice-serviceport-nooverride.yml')
        self.assertEquals(service.config['container']['docker']['portMappings'][0]['servicePort'], 1234)

        # servicePort=1234 in json, override with servicePort=4000 in yaml
        service = lighter.parse_service('src/resources/yaml/staging/myservice-serviceport-override.yml')
        self.assertEquals(service.config['container']['docker']['portMappings'][0]['servicePort'], 4000)

    def _parseErrorPost(self, url, *args, **kwargs):
        if url.startswith('file:'):
            return jsonRequest(url, *args, **kwargs)
        raise self.fail('Should not POST into Marathon')

    def testParseError(self):
        with patch('lighter.util.jsonRequest', wraps=self._parseErrorPost):
            with self.assertRaises(RuntimeError):
                lighter.deploy('http://localhost:1/', filenames=['src/resources/yaml/staging/myservice.yml', 'src/resources/yaml/staging/myservice-broken.yml'])

    def _createJsonRequestWrapper(self, marathonurl='http://localhost:1'):
        appurl = '%s/v2/apps/myproduct/myservice' % marathonurl

        def wrapper(url, method='GET', data=None, *args, **kwargs):
            if url.startswith('file:'):
                return jsonRequest(url, data, *args, **kwargs)
            if url == appurl and method == 'PUT' and data:
                self.assertEquals(data['container']['docker']['image'], 'meltwater/myservice:1.0.0')
                self._called = True
                return {}
            if url == appurl and method == 'GET':
                return {'app': {}}
            return None
        return wrapper

    def testResolveMavenJson(self):
        with patch('lighter.util.jsonRequest', wraps=self._createJsonRequestWrapper()):
            lighter.deploy('http://localhost:1/', filenames=['src/resources/yaml/integration/myservice.yml'])
            self.assertTrue(self._called)

    def testDeprecatedResolveTag(self):
        """
        Checks that version ranges can be resolved in the "version: " tag as well
        """
        with patch('lighter.util.jsonRequest', wraps=self._createJsonRequestWrapper()):
            lighter.deploy('http://localhost:1/', filenames=['src/resources/yaml/integration/myservice-version-range.yml'])
            self.assertTrue(self._called)

    def testMarathonAppNot404(self):
        with patch('lighter.util.jsonRequest', wraps=self._createJsonRequestWrapper('http://defaultmarathon:2')) as m_urlopen:
            resp = Mock()
            resp.read.return_value = '{"message": "no app"}'
            m_urlopen.side_effect = urllib2.HTTPError('', 504, 'no app', {'Content-Type': 'application/json'}, resp)
            with self.assertRaises(RuntimeError):
                lighter.deploy(marathonurl=None, filenames=['src/resources/yaml/integration/myservice.yml'])

    def testMarathonAppURLError(self):
        with patch('lighter.util.jsonRequest', wraps=self._createJsonRequestWrapper('http://defaultmarathon:2')) as m_urlopen:
            m_urlopen.side_effect = urllib2.URLError("hello")
            with self.assertRaises(RuntimeError):
                lighter.deploy(marathonurl=None, filenames=['src/resources/yaml/integration/myservice.yml'])

    def testDefaultMarathonUrl(self):
        with patch('lighter.util.jsonRequest', wraps=self._createJsonRequestWrapper('http://defaultmarathon:2')):
            services = lighter.deploy(marathonurl=None, filenames=['src/resources/yaml/integration/myservice.yml'])
            self.assertTrue(self._called)
            self.assertEquals(1, len(services))
            self.assertEquals('/myproduct/myservice', services[0].id)

    def testNoMarathonUrlDefined(self):
        with patch('lighter.util.jsonRequest', wraps=self._createJsonRequestWrapper()):
            with self.assertRaises(RuntimeError) as cm:
                lighter.deploy(marathonurl=None, filenames=['src/resources/yaml/staging/myservice.yml'])
            self.assertEqual("No Marathon URL defined for service src/resources/yaml/staging/myservice.yml", cm.exception.message)

    def testUnresolvedVariable(self):
        service_yaml = 'src/resources/yaml/integration/myservice-unresolved-variable.yml'
        try:
            lighter.parse_service(service_yaml)
        except RuntimeError as e:
            self.assertEquals(e.message, 'Failed to parse %s with the following message: Variable %%{bvar} not found' % service_yaml)
        else:
            self.fail('Expected RuntimeError')

    def testNonStringEnvvar(self):
        service = lighter.parse_service('src/resources/yaml/integration/myservice-nonstring-envvar.yml')
        self.assertEquals('123', service.config['env']['INTVAR'])
        self.assertEquals('123.456', service.config['env']['FLOATVAR'])
        self.assertEquals('true', service.config['env']['TRUEVAR'])
        self.assertEquals('false', service.config['env']['FALSEVAR'])
        self.assertEquals('{"foobar": [1, 2], "foo": "bar", "bar": 123}', service.config['env']['COMPLEXVAR'])

    def testNonStringEnvkey(self):
        service_yaml = 'src/resources/yaml/integration/myservice-nonstring-envkey.yml'
        try:
            lighter.parse_service(service_yaml)
        except ValueError as e:
            self.assertEquals(e.message, 'Only string dict keys are supported, please use quotes around the key \'True\' in %s' % service_yaml)
        else:
            self.fail('Expected ValueError')

    def testParseNoMavenService(self):
        service = lighter.parse_service('src/resources/yaml/staging/myservice-nomaven.yml', profiles=[PROFILE_1, PROFILE_2])
        self.assertEquals(service.document['hipchat']['token'], 'abc123')
        self.assertEquals(service.config['id'], '/myproduct/myservice-nomaven')
        self.assertEquals(service.config['instances'], 1)
        self.assertEquals(service.config['env']['DATABASE'], 'database:3306')
        self.assertEquals(service.config['container']['docker']['image'], 'meltwater/myservice:latest')

    def testPasswordCheckFail(self):
        with self.assertRaises(RuntimeError):
            lighter.verify_secrets([lighter.parse_service('src/resources/yaml/staging/myservice-password.yml')], enforce=True)

    def testPasswordCheckSucceed(self):
        lighter.verify_secrets([lighter.parse_service('src/resources/yaml/staging/myservice-encrypted-password.yml')], enforce=True)

    def testPasswordCheckSubstringsSucceed(self):
        lighter.verify_secrets([lighter.parse_service('src/resources/yaml/staging/myservice-encrypted-substrings.yml')], enforce=True)

    @patch('logging.warn')
    def testPasswordCheckWarning(self, mock_warn):
        lighter.verify_secrets([lighter.parse_service('src/resources/yaml/staging/myservice-password.yml')], enforce=False)
        self.assertEqual(mock_warn.call_count, 1)
        mock_warn.assert_called_with('Found unencrypted secret in src/resources/yaml/staging/myservice-password.yml: DATABASE_PASSWORD')

    def testConfigHash(self):
        service1 = lighter.parse_service('src/resources/yaml/staging/myservice.yml')
        service2 = lighter.parse_service('src/resources/yaml/staging/myservice.yml')
        self.assertEqual(32, len(service1.config['labels']['com.meltwater.lighter.checksum']))
        self.assertEqual(service1.config['labels']['com.meltwater.lighter.checksum'], service2.config['labels']['com.meltwater.lighter.checksum'])

        service3 = lighter.parse_service('src/resources/yaml/staging/myservice-classifier.yml')
        self.assertNotEqual(service1.config['labels']['com.meltwater.lighter.checksum'], service3.config['labels']['com.meltwater.lighter.checksum'])

    def testWriteServices(self):
        service1 = lighter.parse_service('src/resources/yaml/staging/myservice.yml')
        service2 = lighter.parse_service('src/resources/yaml/staging/myservice-non-docker.yml')

        targetdir = tempfile.mkdtemp(prefix='lighter-deploy_test')
        try:
            lighter.write_services(targetdir, [service1, service2])
            self.assertTrue(os.path.exists('%s/src/resources/yaml/staging/myservice.yml.json' % targetdir))
            self.assertTrue(os.path.exists('%s/src/resources/yaml/staging/myservice-non-docker.yml.json' % targetdir))
        finally:
            shutil.rmtree(targetdir)
