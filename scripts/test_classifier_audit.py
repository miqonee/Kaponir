import unittest

from audit_internal_classifier import attribution, source_span, span_coverage


class ClassifierAuditTests(unittest.TestCase):
    def test_split_name_masks_are_distinguished_from_exact_span_matches(self):
        text='Иван Соколов'
        masks=[{'start':0,'end':len('Иван'.encode()),'surface':'Иван'},
               {'start':len('Иван '.encode()),'end':len(text.encode()),'surface':'Соколов'}]
        report=span_coverage(text,{'Иван Соколов':('PERSON','personal')},[],masks)
        self.assertEqual(report['gold_occurrences_touched'],1)
        self.assertEqual(report['gold_occurrences_full_span_covered'],0)
        self.assertEqual(report['gold_occurrences_letters_digits_covered'],1)

    def test_public_substring_inside_private_path_is_not_counted_as_clean_control_damage(self):
        text='/srv/private/python/bin; python'
        value='/srv/private/python/bin'
        masks=[{'start':0,'end':len(value.encode()),'surface':value}]
        report=span_coverage(text,{value:('PATH','team')},['python'],masks)
        self.assertEqual(report['public_occurrences_inside_gold_excluded'],1)
        self.assertEqual(report['public_clear_occurrences'],1)
        self.assertEqual(report['public_clear_occurrences_masked'],0)

    def test_bad_export_offsets_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'offset_mismatch'):
            source_span('Цена 10 ₽',{'start':0,'end':len('Цена'.encode()),'value':'10 ₽'})

    def test_inflection_lemma_is_not_mistaken_for_the_replaced_source_stem(self):
        text='Основная'
        row={'start':0,'end':len('Основн'.encode()),'surface':'Основн','original':'Основна'}
        self.assertEqual(source_span(text,row,'surface'),(0,len('Основн')))

    def test_boundary_mismatch_is_not_automatically_a_false_positive(self):
        self.assertEqual(attribution((2,5),[(0,8)],'abc',[]),'inside_gold_span')
        self.assertEqual(attribution((0,10),[(2,5)],'whole',[]),'contains_gold_span')
        self.assertEqual(attribution((0,5),[(4,8)],'cross',[]),'overlaps_gold_span')
        self.assertEqual(attribution((0,3),[],'Thu',['Thu']),'public_control')


if __name__=='__main__':unittest.main()
