from __future__ import annotations
import tempfile, unittest
from admin import Admin
from storage import Storage

class AdminDraftTests(unittest.TestCase):
    def test_broadcast_text_draft_does_not_require_id(self):
        file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False);file.close()
        store=Storage(file.name);admin=Admin(store)
        store.set_draft('telegram','1','cast_text',{'target':'telegram','buttons':[]})
        text,buttons=admin.text('telegram','1','Текст рассылки')
        self.assertIn('Предпросмотр',text)
        self.assertEqual(buttons[0][0]['callback_data'],'a:castsend')

if __name__=='__main__':unittest.main()
