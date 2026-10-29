"""Chinese display mappings for staff Student Detail and Applications only.

These helpers never normalize inputs or change field definitions used by storage.
Unknown values, user text, filenames and official names pass through unchanged.
"""

COURSE_TYPE_LABELS = {
    'Foundation': '预科', 'Diploma': '文凭课程', 'Bachelor': '本科',
    'Postgraduate': '研究生', 'Master': '硕士', 'Package': '打包课程',
}
APPLICATION_STATUS_LABELS = {
    'draft': '草稿', 'confirmed': '已确认', 'submitted': '已递交',
    'pending': '待处理', 'accepted': '已接受', 'received': '已收到',
    'offer received': '已收到 Offer', 'offer_received': '已收到 Offer',
    'rejected': '已拒绝', 'declined': '已拒绝', 'closed': '已关闭',
    'withdrawn': '已撤回', 'superseded': '已替代', 'converted': '已转为申请',
    'generated': '已生成', 'shared': '已分享', 'expired': '已过期',
    'active': '有效', 'cancelled': '已取消', 'voided': '已作废',
    'sent_to_parent': '已发送给家长', 'signed': '已签署', 'signed_received': '已收到签署文件',
}
APPLICATION_MATERIAL_LABELS = {
    'StudentPassport': '学生护照', 'FatherPassport': '父亲护照',
    'MotherPassport': '母亲护照', 'GuardianPassport': '监护人护照',
    'Passport': '护照', 'BirthCertificate': '出生证明', 'Transcripts': '成绩单',
    'Vaccination': '疫苗接种记录', 'Award': '获奖证明', 'Others': '其他材料',
    'Other': '其他材料', 'StudentInfo': '学生信息表',
    'SchoolPlanReport': '学校方案报告', 'FinalConfirmedSchoolPlan': '最终确认方案',
    'ParentSignaturePage': '家长签字页', 'SignedContract': '已签署合同',
    'ContractSignatureConfirmationPage': '合同签署确认页',
}


def application_status_label(value):
    return APPLICATION_STATUS_LABELS.get(str(value or '').lower(), value)


def course_type_label(value):
    return COURSE_TYPE_LABELS.get(value, value)


def application_material_label(value):
    return APPLICATION_MATERIAL_LABELS.get(value, value)


def student_stage_display_label(value):
    return {'尚未生成家长表单 Link': '尚未生成家长表单链接'}.get(value, value)


SCHOOL_REPORT_STATUS_LABELS = {
    'draft': '草稿', 'active': '当前有效', 'generated': '已生成', 'shared': '已分享',
    'viewed': '已查看', 'confirmed': '已确认', 'rejected': '家长不同意',
    'superseded': '已被新版本替代', 'revoked': '已撤销', 'expired': '已过期',
    'invalidated': '已作废', 'archived': '已归档',
}
VIEWER_ROLE_LABELS = {'Mother': '母亲', 'Father': '父亲', 'Guardian': '监护人', 'Other': '其他', 'Primary Parent': '主要家长'}
VIEW_EVENT_LABELS = {
    'view': '查看', 'link_opened': '打开链接', 'access_denied': '访问被拒绝',
    'wechat_blocked': '微信身份不匹配', 'wechat_bound': '绑定微信', 'wechat_login': '微信登录',
    'wechat_oauth_started': '开始微信授权', 'wechat_oauth_failed': '微信授权失败',
    'brochure_viewed': '查看宣传册', 'brochure_preview_page': '预览宣传册页面',
    'page_view': '查看页面', 'mask_triggered': '触发遮罩', 'confirmed': '已确认',
    'confirmation_submitted': '递交确认', 'confirmation_archived': '确认文件已归档',
    'confirmation_archive_failed': '确认文件归档失败',
    'visibility_hidden': '页面切至后台', 'viewer_opened': '查看人打开报告',
}


def school_report_status_label(value):
    return SCHOOL_REPORT_STATUS_LABELS.get(str(value or '').lower(), value)


def school_report_title_label(value):
    return '学校方案报告' if value in ('', None, 'School Plan Report') else value


def viewer_role_label(value):
    return VIEWER_ROLE_LABELS.get(value, value)


def view_event_label(value):
    return VIEW_EVENT_LABELS.get(value, value)


def viewer_device_label(value):
    return {'mobile': '移动设备', 'desktop': '电脑'}.get(value, value)


def student_profile_display_value(name, value):
    if isinstance(value, list):
        return [student_profile_display_value(name, item) for item in value]
    if name in {'current_year_level', 'applying_year_level', 'sibling_1_year_level',
                'sibling_2_year_level', 'current_school_year_level'}:
        return {f'Year {n}': f'{n}年级' for n in range(1, 13)}.get(value, value)
    if name == 'preferred_intake_term':
        return {'02': '2月入学', '04': '4月入学', '07': '7月入学',
                '10': '10月入学', 'unsure': '不确定'}.get(value, value)
    if name == 'application_stream':
        return {'school_education': '中小学申请', 'higher_education': '高等教育申请'}.get(value, value)
    if name == 'higher_education_course_type':
        return course_type_label(value)
    if name == 'higher_education_package_combination':
        return {'Foundation + Bachelor': '预科 + 本科', 'Diploma + Bachelor': '文凭课程 + 本科',
                'Foundation + Diploma + Bachelor': '预科 + 文凭课程 + 本科'}.get(value, value)
    if name == 'preferred_cities':
        return {'melbourne': '墨尔本', 'sydney': '悉尼', 'brisbane': '布里斯班',
                'adelaide': '阿德莱德', 'perth': '珀斯', 'canberra': '堪培拉',
                'tasmania': '塔斯马尼亚', 'unsure': '不确定'}.get(value, value)
    return value


def student_profile_field_label(name, field_map):
    label = field_map.get(name, {}).get('label', name)
    return '打包课程组合' if name == 'higher_education_package_combination' else label


def localized_profile_sections(sections):
    result = []
    for section_id, title, fields in sections:
        localized = []
        for name, label, kind, required, options in fields:
            if name == 'higher_education_package_combination':
                label = '打包课程组合'
            if options:
                choices = []
                for raw in options:
                    value, _, text = raw.partition('|')
                    display = student_profile_display_value(name, value)
                    # Existing Chinese display labels take precedence.
                    choices.append(value + '|' + (text or str(display)))
                options = choices
            localized.append((name, label, kind, required, options))
        result.append((section_id, title, localized))
    return result


APPLICATION_ERROR_LABELS = {
    'an inactive identical course already exists; ask an administrator to reactivate or merge it':
        '已存在相同的停用课程，请联系管理员重新启用或合并',
    'only a Draft plan can be confirmed': '只能确认草稿学校方案',
    'higher education cycle is required': '请选择高等教育申请周期',
    'unsupported course_type': '不支持此课程类型',
    'university applications can only be added to a Draft plan': '只能向草稿方案添加大学申请',
    'course is outside selected university': '此课程不属于所选大学',
    'plan item does not exist': '大学申请不存在',
    'only Draft university applications can be removed': '只能移除草稿大学申请',
    'submitted university application cannot be removed': '已递交的大学申请不能移除',
    'selected pathway requires configured segments': '所选升学路径尚未配置课程阶段',
    'pathway terminal segment must match final course level': '升学路径的最后阶段必须与最终课程层级一致',
    'plan must be confirmed before submission': '递交申请前请先确认学校方案',
    'valid submission channel is required': '请选择有效的递交渠道',
    'Package pathway requires configured segments': '打包课程的升学路径尚未配置课程阶段',
    'unsupported course level': '不支持此课程层级',
    'course_name is required': '请填写课程 / 专业', 'intake is required': '请填写入学时间',
}


def application_error_display(message):
    prefix = '系统处理失败：' if str(message).startswith('系统处理失败：') else ''
    text = str(message)[len(prefix):]
    return prefix + APPLICATION_ERROR_LABELS.get(text, text)
